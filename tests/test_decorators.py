from nose.tools import eq_
from fudge import Fake, with_fakes

from fabric import decorators
from nose.tools import assert_true, assert_false, assert_equal


def fake_function(*args, **kwargs):
    """
    Returns a ``fudge.Fake`` exhibiting function-like attributes.

    Passes in all args/kwargs to the ``fudge.Fake`` constructor. However, if
    ``callable`` or ``expect_call`` kwargs are not given, ``callable`` will be
    set to True by default.
    """
    # Must define __name__ to be compatible with function wrapping mechanisms
    # like @wraps().
    if 'callable' not in kwargs and 'expect_call' not in kwargs:
        kwargs['callable'] = True
    return Fake(*args, **kwargs).has_attr(__name__='fake')


@with_fakes
def test_runs_once_runs_only_once():
    """
    @runs_once prevents decorated func from running >1 time
    """
    func = fake_function(expect_call=True).times_called(1)
    task = decorators.runs_once(func)
    for i in range(2):
        task()


def test_runs_once_returns_same_value_each_run():
    """
    @runs_once memoizes return value of decorated func
    """
    return_value = "foo"
    task = decorators.runs_once(fake_function().returns(return_value))
    for i in range(2):
        eq_(task(), return_value)


@decorators.runs_once
def single_run():
    pass

def test_runs_once():
    assert_true(decorators.is_sequential(single_run))
    assert_false(hasattr(single_run, 'return_value'))
    single_run()
    assert_true(hasattr(single_run, 'return_value'))
    assert_equal(None, single_run())


@decorators.runs_sequential
def sequential():
    pass

@decorators.runs_sequential
@decorators.runs_parallel
def sequential2():
    pass

def test_sequential():
    assert_true(decorators.is_sequential(sequential))
    assert_false(decorators.is_parallel(sequential))
    sequential()

    assert_true(decorators.is_sequential(sequential2))
    assert_false(decorators.is_parallel(sequential2))
    sequential2()


@decorators.runs_parallel
def parallel():
    pass

@decorators.runs_parallel
@decorators.runs_sequential
def parallel2():
    pass

def test_parallel():
    assert_true(decorators.is_parallel(parallel))
    assert_false(decorators.is_sequential(parallel))
    parallel() 
    
    assert_true(decorators.is_parallel(parallel2))
    assert_false(decorators.is_sequential(parallel2))
    parallel2()  


@decorators.roles('test')
def use_roles():
    pass

def test_roles():
    assert_true(hasattr(use_roles, 'roles'))
    assert_equal(use_roles.roles, ['test'])


@decorators.hosts('test')
def use_hosts():
    pass

def test_hosts():
    assert_true(hasattr(use_hosts, 'hosts'))
    assert_equal(use_hosts.hosts, ['test'])


def test_needs_multiprocessing():
    assert_true(decorators.needs_multiprocessing())
