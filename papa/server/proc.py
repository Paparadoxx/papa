import os
import sys
import logging
import ctypes
import errno
from papa import utils
from papa.utils import extract_name_value_pairs, wildcard_iter
from subprocess import Popen, PIPE, STDOUT
from threading import Thread, Lock

try:
    import pwd
except ImportError:
    pwd = None

try:
    import grp
except ImportError:
    grp = None

try:
    import resource
except ImportError:
    resource = None

__author__ = 'Scott Maxwell'

logger = logging.getLogger('papa.server')
IS_WINDOWS = os.name == 'nt'


def convert_size_string_to_bytes(s):
    try:
        return int(s)
    except ValueError:
        return int(s[:-1]) * {'g': 1073741824, 'm': 1048576, 'k': 1024}[s[-1].lower()]


class Process(object):
    """Wraps a process.

    Options:

    - **name**: the process name. Multiple processes can share the same name.

    - **args**: the arguments for the command to run. Can be a list or
      a string. If **args** is  a string, it's splitted using
      :func:`shlex.split`. Defaults to None.

    - **working_dir**: the working directory to run the command in. If
      not provided, will default to the current working directory.

    - **shell**: if *True*, will run the command in the shell
      environment. *False* by default. **warning: this is a
      security hazard**.

    - **uid**: if given, is the user id or name the command should run
      with. The current uid is the default.

    - **gid**: if given, is the group id or name the command should run
      with. The current gid is the default.

    - **env**: a mapping containing the environment variables the command
      will run with. Optional.

    - **rlimits**: a mapping containing rlimit names and values that will
      be set before the command runs.
    """
    def __init__(self, name, args, env, rlimits, instance_globals,
                 working_dir=None, shell=False, uid=None, gid=None,
                 out='1m', err='1m'):

        self._processes = instance_globals['processes']

        self.name = name
        self.args = args
        self.env = env
        self.rlimits = rlimits
        self.working_dir = working_dir
        self.shell = shell
        self.pid = 0
        self.out = convert_size_string_to_bytes(out)
        self.err = err if err == 'out' else convert_size_string_to_bytes(err)
        self.outbuf = None
        self.errbuf = None

        if uid:
            if pwd:
                try:
                    self.uid = int(uid)
                    self.username = pwd.getpwuid(self.uid).pw_name
                except KeyError:
                    raise utils.Error('%r is not a valid user id' % uid)
                except ValueError:
                    try:
                        self.username = uid
                        self.uid = pwd.getpwnam(uid).pw_uid
                    except KeyError:
                        raise utils.Error('%r is not a valid user name' % uid)
            else:
                raise utils.Error('uid is not supported on this platform')
        else:
            self.username = None
            self.uid = None

        if gid:
            if grp:
                try:
                    self.gid = int(gid)
                    grp.getgrgid(self.gid)
                except (KeyError, OverflowError):
                    raise utils.Error('No such group: %r' % gid)
                except ValueError:
                    try:
                        self.gid = grp.getgrnam(gid).gr_gid
                    except KeyError:
                        raise utils.Error('No such group: %r' % gid)
            else:
                raise utils.Error('gid is not supported on this platform')
        elif self.uid:
            self.gid = pwd.getpwuid(self.uid).pw_gid
        else:
            self.gid = None

        # sockets created before fork, should be let go after.
        self._sockets = []
        self._worker = None
        self._thread = None
        self._lock = None

    def __eq__(self, other):
        return (
            self.name == other.name and
            self.args == other.args and
            self.env == other.env and
            self.rlimits == other.rlimits and
            self.working_dir == other.working_dir and
            self.shell == other.shell and
            self.out == other.out and
            self.err == other.err and
            self.uid == other.uid and
            self.gid == other.gid
        )

    def spawn(self):
        existing = self._processes.get(self.name)
        if existing:
            if self == existing:
                self.pid = existing.pid
            else:
                raise utils.Error('Process for {0} has already been created - {1}'.format(self.name, str(existing)))
        else:
            fixed_args = []
            for arg in self.args:
                if '$(socket.' in arg:
                    pass
                fixed_args.append(arg)

            def preexec():
                streams = [sys.stdin]
                if not self.out:
                    streams.append(sys.stdout)
                if not self.err:
                    streams.append(sys.stderr)
                for stream in streams:
                    if hasattr(stream, 'fileno'):
                        try:
                            stream.flush()
                            devnull = os.open(os.devnull, os.O_RDWR)
                            # noinspection PyTypeChecker
                            os.dup2(devnull, stream.fileno())
                            # noinspection PyTypeChecker
                            os.close(devnull)
                        except IOError:
                            # some streams, like stdin - might be already closed.
                            pass

                # noinspection PyArgumentList
                os.setsid()

                if resource:
                    for limit, value in self.rlimits.items():
                        resource.setrlimit(limit, (value, value))

                if self.gid:
                    try:
                        # noinspection PyTypeChecker
                        os.setgid(self.gid)
                    except OverflowError:
                        if not ctypes:
                            raise
                        # versions of python < 2.6.2 don't manage unsigned int for
                        # groups like on osx or fedora
                        os.setgid(-ctypes.c_int(-self.gid).value)

                    if self.username is not None:
                        try:
                            # noinspection PyTypeChecker
                            os.initgroups(self.username, self.gid)
                        except (OSError, AttributeError):
                            # not support on Mac or 2.6
                            pass

                if self.uid:
                    # noinspection PyTypeChecker
                    os.setuid(self.uid)

            if IS_WINDOWS:
                # On Windows we can't use a pre-exec function
                preexec_fn = None
            else:
                preexec_fn = preexec

            extra = {}
            if self.out:
                extra['stdout'] = PIPE
                self.outbuf = bytearray(self.out)

            if self.err:
                if self.err == 'out':
                    extra['stderr'] = STDOUT
                else:
                    extra['stderr'] = PIPE
                    self.errbuf = bytearray(self.err)

            self._worker = Popen(fixed_args, cwd=self.working_dir,
                                 shell=self.shell, preexec_fn=preexec_fn,
                                 env=self.env, **extra)

            # let go of sockets created only for self.worker to inherit
            self._sockets = []
            self._processes[self.name] = self
            self.pid = self._worker.pid
            self._lock = Lock()

            self._thread = Thread(target=self._watch)
            self._thread.daemon = True
            self._thread.start()

        return self

    def exists(self):
        try:
            os.kill(self.pid, 0)
            return True
        except OSError as e:
            return e.errno == errno.EPERM
        except Exception:
            return False

    def _watch(self):
        pass

    def __str__(self):
        return '{0} pid={1}'.format(self.name, self.pid)


# noinspection PyUnusedLocal
def process_command(sock, args, instance_globals):
    """Create a process.
You need to specify a name, followed by name=value pairs for the process
options, followed by the command and args to execute. The name must not contain
spaces.

Process options are:
    uid - the username or user ID to use when starting the process
    gid - the group name or group ID to use when starting the process
    working_dir - must be an absolute path if specified
    output - size of each output buffer (default is 1m)

You can also specify environment variables by prefixing the name with 'env.' and
rlimits by prefixing the name with 'rlimit.'

Examples:
    process sf uid=1001 gid=2000 working_dir=/sf/bin/ output=1m /sf/bin/uwsgi --ini uwsgi-live.ini --socket fd://27 --stats 127.0.0.1:8090
    process nginx /usr/local/nginx/sbin/nginx
"""
    name = args.pop(0)
    env = {}
    rlimits = {}
    kwargs = {}
    for key, value in extract_name_value_pairs(args).items():
        if key.startswith('env.'):
            env[key[4:]] = value
        elif key.startswith('rlimit.'):
            key = key[7:]
            try:
                rlimits[getattr(resource, 'RLIMIT_%s' % key.upper())] = int(value)
            except AttributeError:
                raise utils.Error('Unknown rlimit "%s"' % key)
            except ValueError:
                raise utils.Error('The rlimit value for "%s" must be an integer, not "%s"' % (key, value))
        else:
            kwargs[key] = value
    p = Process(name, args, env, rlimits, instance_globals, **kwargs)
    with instance_globals['lock']:
        return str(p.spawn())


# noinspection PyUnusedLocal
def processes_command(sock, args, instance_globals):
    """List all active sockets and processes"""
    with instance_globals['lock']:
        return '\n'.join(sorted('{0}'.format(proc) for _, proc in wildcard_iter(instance_globals['processes'], args)))


# noinspection PyUnusedLocal
def close_output_command(sock, args, instance_globals):
    with instance_globals['lock']:
        pass


def watch_command(sock, args, instance_globals):
    """Watch a process"""
    return 'ok'