"""
This module contains Fab's `main` method plus related subroutines.

`main` is executed as the command line ``fab`` program and takes care of
parsing options and commands, loading the user settings file, loading a
fabfile, and executing the commands given.

The other callables defined in this module are internal only. Anything useful
to individuals leveraging Fabric as a library, should be kept elsewhere.
"""

from collections import defaultdict
from operator import add, isMappingType
from optparse import OptionParser
import os
import sys
import types

from fabric import api, state  # For checking callables against the API, & easy mocking
from fabric.contrib import console, files, project  # Ditto
from fabric.network import denormalize, interpret_host_string, disconnect_all, normalize_to_string
from fabric.state import commands, env_options
from fabric.tasks import Task
from fabric.utils import abort, indent
from job_queue import JobQueue

# One-time calculation of "all internal callables" to avoid doing this on every
# check of a given fabfile callable (in is_classic_task()).
_modules = [api, project, files, console]
_internals = reduce(lambda x, y: x + filter(callable, vars(y).values()),
    _modules,
    []
)

# Module recursion cache
class _ModuleCache(object):
    """
    Set-like object operating on modules and storing __name__s internally.
    """
    def __init__(self):
        self.cache = set()

    def __contains__(self, value):
        return value.__name__ in self.cache

    def add(self, value):
        return self.cache.add(value.__name__)

    def clear(self):
        return self.cache.clear()

_seen = _ModuleCache()


def load_settings(path):
    """
    Take given file path and return dictionary of any key=value pairs found.

    Usage docs are in docs/usage/fab.rst, in "Settings files."
    """
    if os.path.exists(path):
        comments = lambda s: s and not s.startswith("#")
        settings = filter(comments, open(path, 'r'))
        return dict((k.strip(), v.strip()) for k, _, v in
            [s.partition('=') for s in settings])
    # Handle nonexistent or empty settings file
    return {}


def _is_package(path):
    """
    Is the given path a Python package?
    """
    return (
        os.path.isdir(path)
        and os.path.exists(os.path.join(path, '__init__.py'))
    )


def find_fabfile():
    """
    Attempt to locate a fabfile, either explicitly or by searching parent dirs.

    Usage docs are in docs/usage/fabfiles.rst, in "Fabfile discovery."
    """
    # Obtain env value
    names = [state.env.fabfile]
    # Create .py version if necessary
    if not names[0].endswith('.py'):
        names += [names[0] + '.py']
    # Does the name contain path elements?
    if os.path.dirname(names[0]):
        # If so, expand home-directory markers and test for existence
        for name in names:
            expanded = os.path.expanduser(name)
            if os.path.exists(expanded):
                if name.endswith('.py') or _is_package(expanded):
                    return os.path.abspath(expanded)
    else:
        # Otherwise, start in cwd and work downwards towards filesystem root
        path = '.'
        # Stop before falling off root of filesystem (should be platform
        # agnostic)
        while os.path.split(os.path.abspath(path))[1]:
            for name in names:
                joined = os.path.join(path, name)
                if os.path.exists(joined):
                    if name.endswith('.py') or _is_package(joined):
                        return os.path.abspath(joined)
            path = os.path.join('..', path)
    # Implicit 'return None' if nothing was found


def is_classic_task(tup):
    """
    Takes (name, object) tuple, returns True if it's a non-Fab public callable.
    """
    name, func = tup
    return (
        callable(func)
        and (func not in _internals)
        and not name.startswith('_')
    )


def load_fabfile(path, importer=None):
    """
    Import given fabfile path and return (docstring, callables).

    Specifically, the fabfile's ``__doc__`` attribute (a string) and a
    dictionary of ``{'name': callable}`` containing all callables which pass
    the "is a Fabric task" test.
    """
    if importer is None:
        importer = __import__
    # Get directory and fabfile name
    directory, fabfile = os.path.split(path)
    # If the directory isn't in the PYTHONPATH, add it so our import will work
    added_to_path = False
    index = None
    if directory not in sys.path:
        sys.path.insert(0, directory)
        added_to_path = True
    # If the directory IS in the PYTHONPATH, move it to the front temporarily,
    # otherwise other fabfiles -- like Fabric's own -- may scoop the intended
    # one.
    else:
        i = sys.path.index(directory)
        if i != 0:
            # Store index for later restoration
            index = i
            # Add to front, then remove from original position
            sys.path.insert(0, directory)
            del sys.path[i + 1]
    # Perform the import (trimming off the .py)
    imported = importer(os.path.splitext(fabfile)[0])
    # Remove directory from path if we added it ourselves (just to be neat)
    if added_to_path:
        del sys.path[0]
    # Put back in original index if we moved it
    if index is not None:
        sys.path.insert(index + 1, directory)
        del sys.path[0]

    # Actually load tasks
    docstring, new_style, classic, default = load_tasks_from_module(imported)
    tasks = new_style if state.env.new_style_tasks else classic
    # Clean up after ourselves
    _seen.clear()
    return docstring, tasks, default


def load_tasks_from_module(imported):
    """
    Handles loading all of the tasks for a given `imported` module
    """
    # Obey the use of <module>.__all__ if it is present
    imported_vars = vars(imported)
    if "__all__" in imported_vars:
        imported_vars = [(name, imported_vars[name]) for name in \
                         imported_vars if name in imported_vars["__all__"]]
    else:
        imported_vars = imported_vars.items()
    # Return a two-tuple value.  First is the documentation, second is a
    # dictionary of callables only (and don't include Fab operations or
    # underscored callables)
    new_style, classic, default = extract_tasks(imported_vars)
    return imported.__doc__, new_style, classic, default


# For attribute tomfoolery
class _Dict(dict):
    pass


def extract_tasks(imported_vars):
    """
    Handle extracting tasks from a given list of variables
    """
    new_style_tasks = _Dict()
    classic_tasks = {}
    default_task = None
    if 'new_style_tasks' not in state.env:
        state.env.new_style_tasks = False
    for tup in imported_vars:
        name, obj = tup
        if is_task_object(obj):
            state.env.new_style_tasks = True
            # Honor instance.name
            new_style_tasks[obj.name] = obj
            # Handle aliasing
            if obj.aliases is not None:
                for alias in obj.aliases:
                    new_style_tasks[alias] = obj
            # Handle defaults
            if obj.is_default:
                default_task = obj
        elif is_classic_task(tup):
            classic_tasks[name] = obj
        elif is_task_module(obj):
            docs, newstyle, classic, default = load_tasks_from_module(obj)
            for task_name, task in newstyle.items():
                if name not in new_style_tasks:
                    new_style_tasks[name] = _Dict()
                new_style_tasks[name][task_name] = task
            if default is not None:
                new_style_tasks[name].default = default
    return new_style_tasks, classic_tasks, default_task


def is_task_module(a):
    """
    Determine if the provided value is a task module
    """
    #return (type(a) is types.ModuleType and
    #        any(map(is_task_object, vars(a).values())))
    if type(a) is types.ModuleType and a not in _seen:
        # Flag module as seen
        _seen.add(a)
        # Signal that we need to check it out
        return True


def is_task_object(a):
    """
    Determine if the provided value is a ``Task`` object.

    This returning True signals that all tasks within the fabfile
    module must be Task objects.
    """
    return isinstance(a, Task) and a.use_task_objects


def parse_options():
    """
    Handle command-line options with optparse.OptionParser.

    Return list of arguments, largely for use in `parse_arguments`.
    """
    #
    # Initialize
    #

    parser = OptionParser(usage="fab [options] <command>[:arg1,arg2=val2,host=foo,hosts='h1;h2',...] ...")

    #
    # Define options that don't become `env` vars (typically ones which cause
    # Fabric to do something other than its normal execution, such as
    # --version)
    #

    # Version number (optparse gives you --version but we have to do it
    # ourselves to get -V too. sigh)
    parser.add_option('-V', '--version',
        action='store_true',
        dest='show_version',
        default=False,
        help="show program's version number and exit"
    )

    # List Fab commands found in loaded fabfiles/source files
    parser.add_option('-l', '--list',
        action='store_true',
        dest='list_commands',
        default=False,
        help="print list of possible commands and exit"
    )

    # Like --list, but text processing friendly
    parser.add_option('--shortlist',
        action='store_true',
        dest='shortlist',
        default=False,
        help="alias for -F short --list"
    )

    # Control behavior of --list
    LIST_FORMAT_OPTIONS = ('short', 'normal', 'nested')
    parser.add_option('-F', '--list-format',
        choices=LIST_FORMAT_OPTIONS,
        default='normal',
        help="formats --list, choices: %s" % ", ".join(LIST_FORMAT_OPTIONS)
    )

    # Display info about a specific command
    parser.add_option('-d', '--display',
        metavar='COMMAND',
        help="print detailed info about a given command and exit"
    )

    #
    # Add in options which are also destined to show up as `env` vars.
    #

    for option in env_options:
        parser.add_option(option)

    #
    # Finalize
    #

    # Return three-tuple of parser + the output from parse_args (opt obj, args)
    opts, args = parser.parse_args()
    return parser, opts, args

def _is_task(name, value):
    """
    Is the object a task as opposed to e.g. a dict or int?
    """
    return is_classic_task((name, value)) or is_task_object(value)

def _sift_tasks(mapping):
    tasks, collections = [], []
    for name, value in mapping.iteritems():
        if _is_task(name, value):
            tasks.append(name)
        elif isMappingType(value):
            collections.append(name)
    tasks = sorted(tasks)
    collections = sorted(collections)
    return tasks, collections

def _task_names(mapping):
    """
    Flatten & sort task names in a breadth-first fashion.

    Tasks are always listed before submodules at the same level, but within
    those two groups, sorting is alphabetical.
    """
    tasks, collections = _sift_tasks(mapping)
    for collection in collections:
        module = mapping[collection]
        if hasattr(module, 'default'):
            tasks.append(collection)
        join = lambda x: ".".join((collection, x))
        tasks.extend(map(join, _task_names(module)))
    return tasks

def _crawl(name, mapping):
    """
    ``name`` of ``'a.b.c'`` => ``mapping['a']['b']['c']``
    """
    key, _, rest = name.partition('.')
    value = mapping[key]
    if not rest:
        return value
    return _crawl(rest, value)

def crawl(name, mapping):
    try:
        result = _crawl(name, mapping)
        # Handle default tasks
        if isinstance(result, _Dict) and getattr(result, 'default', False):
            result = result.default
        return result
    except (KeyError, TypeError):
        return None

def _print_docstring(docstrings, name):
    if not docstrings:
        return False
    docstring = crawl(name, state.commands).__doc__
    if type(docstring) in types.StringTypes:
        return docstring


def _normal_list(docstrings=True):
    result = []
    task_names = _task_names(state.commands)
    # Want separator between name, description to be straight col
    max_len = reduce(lambda a, b: max(a, len(b)), task_names, 0)
    sep = '  '
    trail = '...'
    for name in task_names:
        output = None
        docstring = _print_docstring(docstrings, name)
        if docstring:
            lines = filter(None, docstring.splitlines())
            first_line = lines[0].strip()
            # Truncate it if it's longer than N chars
            size = 75 - (max_len + len(sep) + len(trail))
            if len(first_line) > size:
                first_line = first_line[:size] + trail
            output = name.ljust(max_len) + sep + first_line
        # Or nothing (so just the name)
        else:
            output = name
        result.append(indent(output))
    return result


def _nested_list(mapping, level=1):
    result = []
    tasks, collections = _sift_tasks(mapping)
    # Tasks come first
    result.extend(map(lambda x: indent(x, spaces=level * 4), tasks))
    for collection in collections:
        module = mapping[collection]
        # Section/module "header"
        result.append(indent(collection + ":", spaces=level * 4))
        # Recurse
        result.extend(_nested_list(module, level + 1))
    return result

COMMANDS_HEADER = "Available commands"
NESTED_REMINDER = " (remember to call as module.[...].task)"

def list_commands(docstring, format_):
    """
    Print all found commands/tasks, then exit. Invoked with ``-l/--list.``

    If ``docstring`` is non-empty, it will be printed before the task list.

    ``format_`` should conform to the options specified in
    ``LIST_FORMAT_OPTIONS``, e.g. ``"short"``, ``"normal"``.
    """
    # Short-circuit with simple short output
    if format_ == "short":
        return _task_names(state.commands)
    # Otherwise, handle more verbose modes
    result = []
    # Docstring at top, if applicable
    if docstring:
        trailer = "\n" if not docstring.endswith("\n") else ""
        result.append(docstring + trailer)
    header = COMMANDS_HEADER
    if format_ == "nested":
        header += NESTED_REMINDER
    result.append(header + ":\n")
    c = _normal_list() if format_ == "normal" else _nested_list(state.commands)
    result.extend(c)
    return result


def display_command(name):
    """
    Print command function's docstring, then exit. Invoked with -d/--display.
    """
    # Sanity check
    command = crawl(name, state.commands)
    if command is None:
        msg = "Task '%s' does not appear to exist. Valid task names:\n%s"
        abort(msg % (name, "\n".join(_normal_list(False))))
    # Print out nicely presented docstring if found
    if command.__doc__:
        print("Displaying detailed information for task '%s':" % name)
        print('')
        print(indent(command.__doc__, strip=True))
        print('')
    # Or print notice if not
    else:
        print("No detailed information available for task '%s':" % name)
    sys.exit(0)


def _escape_split(sep, argstr):
    """
    Allows for escaping of the separator: e.g. task:arg='foo\, bar'

    It should be noted that the way bash et. al. do command line parsing, those
    single quotes are required.
    """
    escaped_sep = r'\%s' % sep

    if escaped_sep not in argstr:
        return argstr.split(sep)

    before, _, after = argstr.partition(escaped_sep)
    startlist = before.split(sep)  # a regular split is fine here
    unfinished = startlist[-1]
    startlist = startlist[:-1]

    # recurse because there may be more escaped separators
    endlist = _escape_split(sep, after)

    # finish building the escaped value. we use endlist[0] becaue the first
    # part of the string sent in recursion is the rest of the escaped value.
    unfinished += sep + endlist[0]

    return startlist + [unfinished] + endlist[1:]  # put together all the parts


def parse_arguments(arguments):
    """
    Parse string list into list of tuples: command, args, kwargs, hosts, roles.

    See docs/usage/fab.rst, section on "per-task arguments" for details.
    """
    cmds = []
    for cmd in arguments:
        args = []
        kwargs = {}
        hosts = []
        roles = []
        exclude_hosts = []
        if ':' in cmd:
            cmd, argstr = cmd.split(':', 1)
            for pair in _escape_split(',', argstr):
                k, _, v = pair.partition('=')
                if _:
                    # Catch, interpret host/hosts/role/roles/exclude_hosts
                    # kwargs
                    if k in ['host', 'hosts', 'role', 'roles', 'exclude_hosts']:
                        if k == 'host':
                            hosts = [v.strip()]
                        elif k == 'hosts':
                            hosts = [x.strip() for x in v.split(';')]
                        elif k == 'role':
                            roles = [v.strip()]
                        elif k == 'roles':
                            roles = [x.strip() for x in v.split(';')]
                        elif k == 'exclude_hosts':
                            exclude_hosts = [x.strip() for x in v.split(';')]
                    # Otherwise, record as usual
                    else:
                        kwargs[k] = v
                else:
                    args.append(k)
        cmds.append((cmd, args, kwargs, hosts, roles, exclude_hosts))
    return cmds


def parse_remainder(arguments):
    """
    Merge list of "remainder arguments" into a single command string.
    """
    return ' '.join(arguments)


def _merge(hosts, roles, exclude=[]):
    """
    Merge given host and role lists into one list of deduped hosts.
    """
    # Abort if any roles don't exist
    bad_roles = [x for x in roles if x not in state.env.roledefs]
    if bad_roles:
        abort("The following specified roles do not exist:\n%s" % (
            indent(bad_roles)
        ))

    # Look up roles, turn into flat list of hosts
    role_hosts = []
    for role in roles:
        value = state.env.roledefs[role]
        # Handle "lazy" roles (callables)
        if callable(value):
            value = value()
        role_hosts += value

    # Return deduped combo of hosts and role_hosts, preserving order within
    # them (vs using set(), which may lose ordering) and skipping hosts to be
    # excluded.
    cleaned_hosts = _clean_hosts(list(hosts) + list(role_hosts))
    all_hosts = []
    for host in cleaned_hosts:
        if host not in all_hosts and host not in exclude:
            all_hosts.append(host)
    return all_hosts

def _clean_hosts(host_list):
    """
    Clean host strings to ensure no trailing whitespace, etc.
    """
    return [host.strip() for host in host_list]

def get_hosts(command, cli_hosts, cli_roles, cli_exclude_hosts):
    """
    Return the host list the given command should be using.

    See :ref:`execution-model` for detailed documentation on how host lists are
    set.
    """
    # Command line per-command takes precedence over anything else.
    if cli_hosts or cli_roles:
        return _merge(cli_hosts, cli_roles, cli_exclude_hosts)
    # Decorator-specific hosts/roles go next
    func_hosts = getattr(command, 'hosts', [])
    func_roles = getattr(command, 'roles', [])
    if func_hosts or func_roles:
        return _merge(func_hosts, func_roles, cli_exclude_hosts)
    # Finally, the env is checked (which might contain globally set lists from
    # the CLI or from module-level code). This will be the empty list if these
    # have not been set -- which is fine, this method should return an empty
    # list if no hosts have been set anywhere.
    return _merge(state.env['hosts'], state.env['roles'], state.env['exclude_hosts'])


def update_output_levels(show, hide):
    """
    Update state.output values as per given comma-separated list of key names.

    For example, ``update_output_levels(show='debug,warnings')`` is
    functionally equivalent to ``state.output['debug'] = True ;
    state.output['warnings'] = True``. Conversely, anything given to ``hide``
    sets the values to ``False``.
    """
    if show:
        for key in show.split(','):
            state.output[key] = True
    if hide:
        for key in hide.split(','):
            state.output[key] = False


def requires_parallel(task):
    """
    Returns True if given ``task`` should be run in parallel mode.

    Specifically:

    * It's been explicitly marked with ``@parallel``, or:
    * It's *not* been explicitly marked with ``@serial`` *and* the global
      parallel option (``env.parallel``) is set to ``True``.
    """
    return (
        (state.env.parallel and not getattr(task, 'serial', False))
        or getattr(task, 'parallel', False)
    )


def _run_task(task, args, kwargs):
    # First, try class-based tasks
    if hasattr(task, 'run') and callable(task.run):
        return task.run(*args, **kwargs)
    # Fallback to callable behavior
    return task(*args, **kwargs)


def _get_pool_size(task, hosts):
    # Default parallel pool size (calculate per-task in case variables
    # change)
    default_pool_size = state.env.pool_size or len(hosts)
    # Allow per-task override
    pool_size = getattr(task, 'pool_size', default_pool_size)
    # But ensure it's never larger than the number of hosts
    pool_size = min((pool_size, len(hosts)))
    # Inform user of final pool size for this task
    if state.output.debug:
        msg = "Parallel tasks now using pool size of %d"
        print msg % state.env.pool_size
    return pool_size


def _parallel_tasks(commands_to_run):
    return any(map(
        lambda x: requires_parallel(crawl(x[0], state.commands)),
        commands_to_run
    ))


def main():
    """
    Main command-line execution loop.
    """
    try:
        # Parse command line options
        parser, options, arguments = parse_options()

        # Handle regular args vs -- args
        arguments = parser.largs
        remainder_arguments = parser.rargs

        # Update env with any overridden option values
        # NOTE: This needs to remain the first thing that occurs
        # post-parsing, since so many things hinge on the values in env.
        for option in env_options:
            state.env[option.dest] = getattr(options, option.dest)

        # Handle --hosts, --roles, --exclude-hosts (comma separated string =>
        # list)
        for key in ['hosts', 'roles', 'exclude_hosts']:
            if key in state.env and isinstance(state.env[key], basestring):
                state.env[key] = state.env[key].split(',')

        # Handle output control level show/hide
        update_output_levels(show=options.show, hide=options.hide)

        # Handle version number option
        if options.show_version:
            print("Fabric %s" % state.env.version)
            sys.exit(0)

        # Load settings from user settings file, into shared env dict.
        state.env.update(load_settings(state.env.rcfile))

        # Find local fabfile path or abort
        fabfile = find_fabfile()
        if not fabfile and not remainder_arguments:
            abort("""Couldn't find any fabfiles!

Remember that -f can be used to specify fabfile path, and use -h for help.""")

        # Store absolute path to fabfile in case anyone needs it
        state.env.real_fabfile = fabfile

        # Load fabfile (which calls its module-level code, including
        # tweaks to env values) and put its commands in the shared commands
        # dict
        if fabfile:
            docstring, callables, default = load_fabfile(fabfile)
            state.commands.update(callables)

        # Handle case where we were called bare, i.e. just "fab", and print
        # a help message.
        actions = (options.list_commands, options.shortlist, options.display,
            arguments, remainder_arguments, default)
        if not any(actions):
            parser.print_help()
            sys.exit(1)

        # Abort if no commands found
        if not state.commands and not remainder_arguments:
            abort("Fabfile didn't contain any commands!")

        # Now that we're settled on a fabfile, inform user.
        if state.output.debug:
            if fabfile:
                print("Using fabfile '%s'" % fabfile)
            else:
                print("No fabfile loaded -- remainder command only")

        # Shortlist is now just an alias for the "short" list format;
        # it overrides use of --list-format if somebody were to specify both
        if options.shortlist:
            options.list_format = 'short'
            options.list_commands = True

        # List available commands
        if options.list_commands:
            print("\n".join(list_commands(docstring, options.list_format)))
            sys.exit(0)

        # Handle show (command-specific help) option
        if options.display:
            display_command(options.display)

        # If user didn't specify any commands to run, show help
        if not (arguments or remainder_arguments or default):
            parser.print_help()
            sys.exit(0)  # Or should it exit with error (1)?

        # Parse arguments into commands to run (plus args/kwargs/hosts)
        commands_to_run = parse_arguments(arguments)

        # Parse remainders into a faux "command" to execute
        remainder_command = parse_remainder(remainder_arguments)

        # Figure out if any specified task names are invalid
        unknown_commands = []
        for tup in commands_to_run:
            if crawl(tup[0], state.commands) is None:
                unknown_commands.append(tup[0])

        # Abort if any unknown commands were specified
        if unknown_commands:
            abort("Command(s) not found:\n%s" \
                % indent(unknown_commands))

        # Generate remainder command and insert into commands, commands_to_run
        if remainder_command:
            r = '<remainder>'
            state.commands[r] = lambda: api.run(remainder_command)
            commands_to_run.append((r, [], {}, [], [], []))

        # Ditto for a default, if found
        if not commands_to_run and default:
            commands_to_run.append((default.name, [], {}, [], [], []))

        if state.output.debug:
            names = ", ".join(x[0] for x in commands_to_run)
            print("Commands to run: %s" % names)

        # Import multiprocessing if needed, erroring out usefully if it can't.
        if state.env.parallel or _parallel_tasks(commands_to_run):
            try:
                import multiprocessing
            except ImportError, e:
                msg = "At least one task needs to be run in parallel, but the\nmultiprocessing module cannot be imported:"
                msg += "\n\n\t%s\n\n" % e
                msg += "Please make sure the module is installed or that the above ImportError is\nfixed."
                abort(msg)

        # At this point all commands must exist, so execute them in order.
        for name, args, kwargs, cli_hosts, cli_roles, cli_exclude_hosts in commands_to_run:
            # Get callable by itself:
            task = crawl(name, state.commands)
            # Set current command name (used for some error messages)
            state.env.command = name
            # Set host list (also copy to env)
            state.env.all_hosts = hosts = get_hosts(
                task, cli_hosts, cli_roles, cli_exclude_hosts)

            if state.output.debug:
                print "Number for pool: %d" % state.env.pool_size

            if hasattr(task, '_pool_size') and task._pool_size:
                pool_size = task._pool_size

            elif not state.env.pool_size:
                if state.output.debug:
                    print "Since zero make number of hosts: %d" % len(hosts)
                pool_size = len(hosts)

            else:
                pool_size = state.env.pool_size

            if len(hosts) < pool_size:
                pool_size = int(len(hosts)/2) + 1


            jobs = Job_Queue(pool_size)
            if state.output.debug:
                jobs._debug = True

           # If hosts found, execute the function on each host in turn
            for host in hosts:
                # Preserve user
                prev_user = state.env.user
                # Split host string and apply to env dict
                username, hostname, port = interpret_host_string(host)
                # Log to stdout
                if state.output.running and not hasattr(task, 'return_value'):
                    print("[%s] Executing task '%s'" % (host, name))

                if running_parallel(task):
                    #parallel

                    # Actually run command
                    # run in parallel when set globally or on function with decorator
                    if hasattr(task, 'run') and callable(task.run):
                        p = multiprocessing.Process(
                                target = task.run,
                                args = args,
                                kwargs = kwargs,
                                )
                    else: 
                        p = multiprocessing.Process(
                                target = task,
                                args = args,
                                kwargs = kwargs,
                                )
                    # Cast to a str since Unicode strings will fail multiprocessing's isinstance check (https://github.com/goosemo/fabric/issues/11)
                    p.name = str(state.env.host_string)
                    jobs.append(p)

                else:
                    #sequential
                    _run_task(task, args, kwargs)

                # Put old user back
                state.env.user = prev_user

            # If running in parallel, block until job queue is emptied
            if jobs:
                jobs.close()
                jobs.start()

            # If no hosts found, assume local-only and run once
            if not hosts:
                _run_task(task, args, kwargs)

        # If we got here, no errors occurred, so print a final note.
        if state.output.status:
            print("\nDone.")
    except SystemExit:
        # a number of internal functions might raise this one.
        raise
    except KeyboardInterrupt:
        if state.output.status:
            print >> sys.stderr, "\nStopped."
        sys.exit(1)
    except:
        sys.excepthook(*sys.exc_info())
        # we might leave stale threads if we don't explicitly exit()
        sys.exit(1)
    finally:
        disconnect_all()
    sys.exit(0)
