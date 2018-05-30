# coding=utf-8
"""
bot.py - Sopel IRC Bot
Copyright 2008, Sean B. Palmer, inamidst.com
Copyright 2012, Edward Powell, http://embolalia.net
Copyright © 2012, Elad Alfassa <elad@fedoraproject.org>
Licensed under the Eiffel Forum License 2.
http://sopel.dftba.net/
"""

import time
import imp
import os
import re
import socket
import threading

from datetime import datetime
from sopel import tools
import irc
from db import SopelDB
from tools import (stderr, Nick, PriorityQueue, released,
                   get_command_regexp)
import module


class Sopel(irc.Bot):
    NOLIMIT = module.NOLIMIT

    def __init__(self, config):
        irc.Bot.__init__(self, config.core)
        self.config = config
        """The ``Config`` for the current Sopel instance."""
        self.doc = {}
        """
        A dictionary of command names to their docstring and example, if
        declared. The first item in a callable's commands list is used as the
        key in version *3.2* onward. Prior to *3.2*, the name of the function
        as declared in the source code was used.
        """
        self.stats = {}
        """
        A dictionary which maps a tuple of a function name and where it was
        used to the nuber of times it was used there.
        """
        self.times = {}
        """
        A dictionary mapping lower-case'd nicks to dictionaries which map
        funtion names to the time which they were last used by that nick.
        """
        self.acivity = {}

        self.server_capabilities = set()
        """A set containing the IRCv3 capabilities that the server supports.
        For servers that do not support IRCv3, this will be an empty set."""
        self.enabled_capabilities = set()
        """A set containing the IRCv3 capabilities that the bot has enabled."""
        self._cap_reqs = dict()
        """A dictionary of capability requests
        Maps the capability name to a tuple of the prefix ('-', '=', or ''),
        the name of the requesting module, and the function to call if the
        request is rejected."""

        self.privileges = dict()
        """A dictionary of channels to their users and privilege levels
        The value associated with each channel is a dictionary of Nicks to a
        bitwise integer value, determined by combining the appropriate constants
        from `module`."""

        self.db = SopelDB(config)
        if self.db.check_table('locales', ['name'], 'name'):
            self.settings = self.db.locales
            self.db.preferences = self.db.locales
        elif self.db.check_table('preferences', ['name'], 'name'):
            self.settings = self.db.preferences
        elif self.db.type is not None:
            self.db.add_table('preferences', ['name'], 'name')
            self.settings = self.db.preferences

        self.memory = tools.SopelMemory()
        """
        A thread-safe dict for storage of runtime data to be shared between
        modules. See `SopelMemory <#tools.Sopel.SopelMemory>`_
        """

        self.scheduler = Sopel.JobScheduler(self)
        self.scheduler.start()

        #Set up block lists
        #Default to empty
        if not self.config.core.nick_blocks:
            self.config.core.nick_blocks = []
        if not self.config.core.nick_blocks:
            self.config.core.host_blocks = []
        #Add nicks blocked under old scheme, if present
        if self.config.core.other_bots:
            nicks = self.config.core.get_list('nick_blocks')
            bots = self.config.core.get_list('other_bots')
            nicks.extend(bots)
            self.config.core.nick_blocks = nicks
            self.config.core.other_bots = False
            self.config.save()

        self.setup()

    class JobScheduler(threading.Thread):
        """Calls jobs assigned to it in steady intervals.
        JobScheduler is a thread that keeps track of Jobs and calls them
        every X seconds, where X is a property of the Job. It maintains jobs
        in a priority queue, where the next job to be called is always the
        first item. Thread safety is maintained with a mutex that is released
        during long operations, so methods add_job and clear_jobs can be
        safely called from the main thread.
        """
        min_reaction_time = 30.0  # seconds
        """How often should scheduler checks for changes in the job list."""

        def __init__(self, bot):
            """Requires bot as argument for logging."""
            threading.Thread.__init__(self)
            self.bot = bot
            self._jobs = PriorityQueue()
            # While PriorityQueue it self is thread safe, this mutex is needed
            # to stop old jobs being put into new queue after clearing the
            # queue.
            self._mutex = threading.Lock()
            # self.cleared is used for more fine grained locking.
            self._cleared = False

        def add_job(self, job):
            """Add a Job to the current job queue."""
            self._jobs.put(job)

        def clear_jobs(self):
            """Clear current Job queue and start fresh."""
            if self._jobs.empty():
                # Guards against getting stuck waiting for self._mutex when
                # thread is waiting for self._jobs to not be empty.
                return
            with self._mutex:
                self._cleared = True
                self._jobs = PriorityQueue()

        def run(self):
            """Run forever."""
            while True:
                try:
                    self._do_next_job()
                except Exception:
                    # Modules exceptions are caught earlier, so this is a bit
                    # more serious. Options are to either stop the main thread
                    # or continue this thread and hope that it won't happen
                    # again.
                    self.bot.error()
                    # Sleep a bit to guard against busy-looping and filling
                    # the log with useless error messages.
                    time.sleep(10.0)  # seconds

        def _do_next_job(self):
            """Wait until there is a job and do it."""
            with self._mutex:
                # Wait until the next job should be executed.
                # This has to be a loop, because signals stop time.sleep().
                while True:
                    job = self._jobs.peek()
                    difference = job.next_time - time.time()
                    duration = min(difference, self.min_reaction_time)
                    if duration <= 0:
                        break
                    with released(self._mutex):
                        time.sleep(duration)

                self._cleared = False
                job = self._jobs.get()
                with released(self._mutex):
                    if job.func.thread:
                        t = threading.Thread(
                            target=self._call, args=(job.func,)
                        )
                        t.start()
                    else:
                        self._call(job.func)
                    job.next()
                # If jobs were cleared during the call, don't put an old job
                # into the new job queue.
                if not self._cleared:
                    self._jobs.put(job)

        def _call(self, func):
            """Wrapper for collecting errors from modules."""
            # Sopel.bot.call is way too specialized to be used instead.
            try:
                func(self.bot)
            except Exception:
                self.bot.error()

    class Job(object):
        """
        Job is a simple structure that hold information about
        when a function should be called next. They can be put in
        a priority queue, in which case the Job that should be
        executed next is returned.
        Calling the method next modifies the Job object for the
        next time it should be executed. Current time is used to
        decide when the job should be executed next so it should
        only be called right after the function was called.
        """

        max_catchup = 5
        """
        This governs how much the scheduling of jobs is allowed
        to get behind before they are simply thrown out to avoid
        calling the same function too many times at once.
        """

        def __init__(self, interval, func):
            """Initialize Job.
            Args:
                interval: number of seconds between calls to func
                func: function to be called
            """
            self.next_time = time.time() + interval
            self.interval = interval
            self.func = func

        def next(self):
            """Update self.next_time with the assumption func was just called.
            Returns: A modified job object.
            """
            last_time = self.next_time
            current_time = time.time()
            delta = last_time + self.interval - current_time

            if last_time > current_time + self.interval:
                # Clock appears to have moved backwards. Reset
                # the timer to avoid waiting for the clock to
                # catch up to whatever time it was previously.
                self.next_time = current_time + self.interval
            elif delta < 0 and abs(delta) > self.interval * self.max_catchup:
                # Execution of jobs is too far behind. Give up on
                # trying to catch up and reset the time, so that
                # will only be repeated a maximum of
                # self.max_catchup times.
                self.next_time = current_time - \
                    self.interval * self.max_catchup
            else:
                self.next_time = last_time + self.interval

            return self

        def __cmp__(self, other):
            """Compare Job objects according to attribute next_time."""
            return self.next_time - other.next_time

        def __str__(self):
            """Return a string representation of the Job object.
            Example result:
                <Job(2013-06-14 11:01:36.884000, 20s, <function upper at 0x02386BF0>)>
            """
            iso_time = str(datetime.fromtimestamp(self.next_time))
            return "<Job(%s, %ss, %s)>" % \
                (iso_time, self.interval, self.func)

        def __iter__(self):
            """This is an iterator. Never stops though."""
            return self

    def setup(self):
        stderr(u"\nWelcome to MasterBot launcher. Loading modules...\n\n")
        self.callables = set()
        self.shutdown_methods = set()

        filenames = self.config.enumerate_modules()
        # Coretasks is special. No custom user coretasks.
        this_dir = os.path.dirname(os.path.abspath(__file__))
        filenames['coretasks'] = os.path.join(this_dir, 'coretasks.py')

        modules = []
        error_count = 0
        for name, filename in filenames.iteritems():
            try:
                module = imp.load_source(name, filename)
            except Exception, e:
                error_count = error_count + 1
                filename, lineno = tools.get_raising_file_and_line()
                rel_path = os.path.relpath(filename, os.path.dirname(__file__))
                raising_stmt = "%s:%d" % (rel_path, lineno)
                stderr("Error loading %s: %s (%s)" % (name, e, raising_stmt))
            else:
                try:
                    if hasattr(module, 'setup'):
                        module.setup(self)
                    self.register(vars(module))
                    modules.append(name)
                except Exception, e:
                    error_count = error_count + 1
                    filename, lineno = tools.get_raising_file_and_line()
                    rel_path = os.path.relpath(
                        filename, os.path.dirname(__file__)
                    )
                    raising_stmt = "%s:%d" % (rel_path, lineno)
                    stderr(u"Error in the configuration protocol %s: %s (%s)"
                           % (name, e, raising_stmt))

        if modules:
            stderr(u'\n\n%d registered modules,' % (len(modules) - 1))
            stderr(u'%d unloaded modules\n\n' % error_count)
        else:
            stderr(u"Warning: I couldn't find any modules!")

        self.bind_commands()

    @staticmethod
    def is_callable(obj):
        """Return true if object is a sopel callable.
        Object must be both be callable and have hashable. Furthermore, it must
        have either "commands", "rule" or "interval" as attributes to mark it
        as a sopel callable.
        """
        if not callable(obj):
            # Check is to help distinguish between sopel callables and objects
            # which just happen to have parameter commands or rule.
            return False
        if (hasattr(obj, 'commands') or
                hasattr(obj, 'rule') or
                hasattr(obj, 'interval')):
            return True
        return False

    @staticmethod
    def is_shutdown(obj):
        """Return true if object is a sopel shutdown method.
        Object must be both be callable and named shutdown.
        """
        if (callable(obj) and
                hasattr(obj, "name")
                and obj.__name__ == 'shutdown'):
            return True
        return False

    def register(self, variables):
        """
        With the ``__dict__`` attribute from a Sopel module, update or add the
        trigger commands and rules, to allow the function to be triggered, and
        shutdown methods, to allow the modules to be notified when sopel is
        quitting.
        """
        for obj in variables.itervalues():
            if self.is_callable(obj):
                self.callables.add(obj)
            if self.is_shutdown(obj):
                self.shutdown_methods.add(obj)

    def unregister(self, variables):
        """Unregister all sopel callables in variables, and their bindings.
        When unloading a module, this ensures that the unloaded modules will
        not get called and that the objects can be garbage collected. Objects
        that have not been registered are ignored.
        Args:
        variables -- A list of callable objects from a sopel module.
        """

        def remove_func(func, commands):
            """Remove all traces of func from commands."""
            for func_list in commands.itervalues():
                if func in func_list:
                    func_list.remove(func)

        hostmask = "%s!%s@%s" % (self.nick, self.user, socket.gethostname())
        sopel = self.SopelWrapper(self, irc.Origin(self, hostmask, [], {}))
        for obj in variables.itervalues():
            if obj in self.callables:
                self.callables.remove(obj)
                for commands in self.commands.itervalues():
                    remove_func(obj, commands)
            if obj in self.shutdown_methods:
                try:
                    obj(sopel)
                except Exception as e:
                    stderr(
                        "Error calling shutdown method for module %s:%s" %
                        (obj.__module__, e)
                    )
                self.shutdown_methods.remove(obj)

    def bind_commands(self):
        self.commands = {'high': {}, 'medium': {}, 'low': {}}
        self.scheduler.clear_jobs()

        def bind(self, priority, regexp, func):
            # Function name is no longer used for anything, as far as I know,
            # but we're going to keep it around anyway.
            if not hasattr(func, 'name'):
                func.name = func.__name__
            # At least for now, only account for the first command listed.
            if func.__doc__ and hasattr(func, 'commands') and func.commands[0]:
                if hasattr(func, 'example'):
                    if isinstance(func.example, basestring):
                        # Support old modules that add the attribute directly.
                        example = func.example
                    else:
                        # The new format is a list of dicts.
                        example = func.example[0]["example"]
                    example = example.replace('$nickname', str(self.nick))
                else:
                    example = None
                self.doc[func.commands[0]] = (func.__doc__, example)
            self.commands[priority].setdefault(regexp, []).append(func)

        def sub(pattern, self=self):
            # These replacements have significant order
            pattern = pattern.replace(
                '$nickname', r'%s' %
                re.escape(self.nick)
            )
            return pattern.replace('$nick', r'%s[,:] +' % re.escape(self.nick))

        for func in self.callables:
            if not hasattr(func, 'unblockable'):
                func.unblockable = False

            if not hasattr(func, 'priority'):
                func.priority = 'medium'

            if not hasattr(func, 'thread'):
                func.thread = True

            if not hasattr(func, 'event'):
                func.event = 'PRIVMSG'
            else:
                func.event = func.event.upper()

            if not hasattr(func, 'rate'):
                if hasattr(func, 'commands'):
                    func.rate = 0
                else:
                    func.rate = 0

            if hasattr(func, 'rule'):
                rules = func.rule
                if isinstance(rules, basestring):
                    rules = [func.rule]

                if isinstance(rules, list):
                    for rule in rules:
                        pattern = sub(rule)
                        flags = re.IGNORECASE
                        if rule.find("\n") != -1:
                            flags |= re.VERBOSE
                        regexp = re.compile(pattern, flags)
                        bind(self, func.priority, regexp, func)

                elif isinstance(func.rule, tuple):
                    # 1) e.g. ('$nick', '(.*)')
                    if len(func.rule) == 2 and isinstance(func.rule[0], str):
                        prefix, pattern = func.rule
                        prefix = sub(prefix)
                        regexp = re.compile(prefix + pattern, re.I)
                        bind(self, func.priority, regexp, func)

                    # 2) e.g. (['p', 'q'], '(.*)')
                    elif len(func.rule) == 2 and \
                            isinstance(func.rule[0], list):
                        prefix = self.config.core.prefix
                        commands, pattern = func.rule
                        for command in commands:
                            command = r'(%s)\b(?: +(?:%s))?' % (
                                command, pattern
                            )
                            regexp = re.compile(prefix + command, re.I)
                            bind(self, func.priority, regexp, func)

                    # 3) e.g. ('$nick', ['p', 'q'], '(.*)')
                    elif len(func.rule) == 3:
                        prefix, commands, pattern = func.rule
                        prefix = sub(prefix)
                        for command in commands:
                            command = r'(%s) +' % command
                            regexp = re.compile(
                                prefix + command + pattern, re.I
                            )
                            bind(self, func.priority, regexp, func)

            if hasattr(func, 'commands'):
                for command in func.commands:
                    prefix = self.config.core.prefix
                    regexp = get_command_regexp(prefix, command)
                    bind(self, func.priority, regexp, func)

            if hasattr(func, 'interval'):
                for interval in func.interval:
                    job = Sopel.Job(interval, func)
                    self.scheduler.add_job(job)

    class SopelWrapper(object):
        def __init__(self, sopel, origin):
            self.bot = sopel
            self.origin = origin

        def say(self, string, max_messages=1):
            self.bot.msg(self.origin.sender, string, max_messages)

        def reply(self, string):
            if isinstance(string, str):
                string = string.decode('utf8')
            self.bot.msg(
                self.origin.sender,
                u'%s: %s' % (self.origin.nick, string)
            )

        def action(self, string, recipient=None):
            if recipient is None:
                recipient = self.origin.sender
            self.bot.msg(recipient, '\001ACTION %s\001' % string)

        def __getattr__(self, attr):
            return getattr(self.bot, attr)

    class Trigger(unicode):
        def __new__(cls, text, origin, bytes, match, event, args, self):
            s = unicode.__new__(cls, text)
            s.sender = origin.sender

            """
            The channel (or nick, in a private message) from which the
            message was sent.
            """
            s.hostmask = origin.hostmask
            """
            Hostmask of the person who sent the message in the form
            <nick>!<user>@<host>
            """
            s.user = origin.user
            """Local username of the person who sent the message"""
            s.nick = origin.nick
            """The ``Nick`` of the person who sent the message."""
            s.event = event
            """
            The IRC event (e.g. ``PRIVMSG`` or ``MODE``) which triggered the
            message."""
            s.bytes = bytes
            """
            The text which triggered the message. Equivalent to
            ``Trigger.group(0)``.
            """
            s.match = match
            """
            The regular expression ``MatchObject_`` for the triggering line.
            .. _MatchObject: http://docs.python.org/library/re.html#match-objects
            """
            s.group = match.group
            """The ``group`` function of the ``match`` attribute.
            See Python ``re_`` documentation for details."""
            s.groups = match.groups
            """The ``groups`` function of the ``match`` attribute.
            See Python ``re_`` documentation for details."""
            s.args = args
            """
            A tuple containing each of the arguments to an event. These are the
            strings passed between the event name and the colon. For example,
            setting ``mode -m`` on the channel ``#example``, args would be
            ``('#example', '-m')``
            """
            s.tags = origin.tags
            """A map of the IRCv3 message tags on the message.
            If the message had no tags, or the server does not support IRCv3
            message tags, this will be an empty dict."""
            if len(self.config.core.get_list('admins')) > 0:
                s.admin = (origin.nick in
                           [Nick(n) for n in
                            self.config.core.get_list('admins')])
            else:
                s.admin = False

            """
            True if the nick which triggered the command is in Sopel's admin
            list as defined in the config file.
            """

            # Support specifying admins by hostnames
            if not s.admin and len(self.config.core.get_list('admins')) > 0:
                for each_admin in self.config.core.get_list('admins'):
                    re_admin = re.compile(each_admin)
                    if re_admin.findall(origin.host):
                        s.admin = True
                    elif '@' in each_admin:
                        temp = each_admin.split('@')
                        re_host = re.compile(temp[1])
                        if re_host.findall(origin.host):
                            s.admin = True

            if not self.config.core.owner:
                s.owner = False
            elif '@' in self.config.core.owner:
                s.owner = origin.nick + '@' + \
                    origin.host == self.config.core.owner
            else:
                s.owner = (origin.nick == Nick(self.config.core.owner))

            # Bot owner inherits all the admin rights, therefore is considered
            # admin
            s.admin = s.admin or s.owner

            s.host = origin.host
            if s.sender is not s.nick:  # no ops in PM
                s.ops = self.ops.get(s.sender, [])
                """
                List of channel operators in the channel the message was
                recived in
                """
                s.halfplus = self.halfplus.get(s.sender, [])
                """
                List of channel half-operators in the channel the message was
                recived in
                """
                s.isop = (s.nick in s.ops or
                          s.nick in s.halfplus)
                """True if the user is half-op or an op"""
                s.voices = self.voices.get(s.sender, [])
                """
                List of channel operators in the channel the message was
                recived in
                """
                s.isvoice = (s.nick in s.ops or
                             s.nick in s.halfplus or
                             s.nick in s.voices)
                """True if the user is voiced, has op, or has half-op"""
            else:
                s.isop = False
                s.isvoice = False
                s.ops = []
                s.halfplus = []
                s.voices = []
            return s

    def call(self, func, origin, sopel, trigger):
        nick = trigger.nick
        if nick not in self.times:
            self.times[nick] = dict()

        if not trigger.admin and \
                not func.unblockable and \
                func in self.times[nick]:
            timediff = time.time() - self.times[nick][func]
            if timediff < func.rate:
                self.times[nick][func] = time.time()
                self.debug(
                    __file__,
                    "%s prevented from using %s in %s: %d < %d" % (
                        trigger.nick, func.__name__, trigger.sender,
                        timediff, func.rate
                    ),
                    "verbose"
                )
                return

        try:
            exit_code = func(sopel, trigger)
        except Exception:
            exit_code = None
            self.error(origin, trigger)

        if exit_code != module.NOLIMIT:
            self.times[nick][func] = time.time()

    def limit(self, origin, func):
        if origin.sender and origin.sender.startswith('#'):
            if self.config.has_section('limit'):
                limits = self.config.limit.get(origin.sender)
                if limits and (func.__module__ not in limits):
                    return True
        return False

    def dispatch(self, origin, text, args):
        event, args = args[0], args[1:]

        wrapper = self.SopelWrapper(self, origin)

        if self.config.core.nick_blocks or self.config.core.host_blocks:
            nick_blocked = self._nick_blocked(origin.nick)
            host_blocked = self._host_blocked(origin.host)
        else:
            nick_blocked = host_blocked = None

        list_of_blocked_functions = []
        for priority in ('high', 'medium', 'low'):
            items = self.commands[priority].items()

            for regexp, funcs in items:
                match = regexp.match(text)
                if not match:
                    continue
                trigger = self.Trigger(
                    text, origin, text, match, event, args, self
                )

                for func in funcs:
                    if (not trigger.admin and
                            not func.unblockable and
                            (nick_blocked or host_blocked)):
                        function_name = "%s.%s" % (
                            func.__module__, func.__name__
                        )
                        list_of_blocked_functions.append(function_name)
                        continue

                    if event != func.event:
                        continue
                    if self.limit(origin, func):
                        continue
                    if func.thread:
                        targs = (func, origin, wrapper, trigger)
                        t = threading.Thread(target=self.call, args=targs)
                        t.start()
                    else:
                        self.call(func, origin, wrapper, trigger)

        if list_of_blocked_functions:
            if nick_blocked and host_blocked:
                block_type = 'both'
            elif nick_blocked:
                block_type = 'nick'
            else:
                block_type = 'host'
            self.debug(
                __file__,
                "[%s]%s prevented from using %s." % (
                    block_type,
                    origin.nick,
                    ', '.join(list_of_blocked_functions)
                ),
                "verbose"
            )

    def _host_blocked(self, host):
        bad_masks = self.config.core.get_list('host_blocks')
        for bad_mask in bad_masks:
            bad_mask = bad_mask.strip()
            if not bad_mask:
                continue
            if (re.match(bad_mask + '$', host, re.IGNORECASE) or
                    bad_mask == host):
                return True
        return False

    def _nick_blocked(self, nick):
        bad_nicks = self.config.core.get_list('nick_blocks')
        for bad_nick in bad_nicks:
            bad_nick = bad_nick.strip()
            if not bad_nick:
                continue
            if (re.match(bad_nick + '$', nick, re.IGNORECASE) or
                    Nick(bad_nick) == nick):
                return True
        return False

    def debug(self, tag, text, level):
        """Sends an error to Sopel's configured ``debug_target``.
        Args:
            tag - What the msg will be tagged as. It is recommended to pass
                __file__ as the tag. If the file exists, a relative path is
                used as the file. Otherwise the tag is used as it is.
            text - Body of the message.
            level - Either verbose, warning or always. Configuration option
                config.verbose which levels are ignored.
        Returns: True if message was sent.
        """
        if not self.config.core.verbose:
            self.config.core.verbose = 'warning'
        if not self.config.core.debug_target:
            self.config.core.debug_target = 'stdio'
        debug_target = self.config.core.debug_target
        verbosity = self.config.core.verbose

        if os.path.exists(tag):
            tag = os.path.relpath(tag, os.path.dirname(__file__))
        debug_msg = "[%s] %s" % (tag, text)

        output_on = {
            'verbose': ['verbose'],
            'warning': ['verbose', 'warning'],
            'always': ['verbose', 'warning', 'always'],
        }
        if level in output_on and verbosity in output_on[level]:
            if debug_target == 'stdio':
                print debug_msg
            else:
                self.msg(debug_target, debug_msg)
            return True
        else:
            return False

    def _shutdown(self):
        stderr(
            'Calling shutdown for %d modules.' % (len(self.shutdown_methods),)
        )

        hostmask = "%s!%s@%s" % (self.nick, self.user, socket.gethostname())
        sopel = self.SopelWrapper(self, irc.Origin(self, hostmask, [], {}))
        for shutdown_method in self.shutdown_methods:
            try:
                stderr(
                    "calling %s.%s" % (
                        shutdown_method.__module__, shutdown_method.__name__,
                    )
                )
                shutdown_method(sopel)
            except Exception as e:
                stderr(
                    "Error calling shutdown method for module %s:%s" % (
                        shutdown_method.__module__, e
                    )
                )

    def cap_req(self, module_name, capability, failure_callback):
        """Tell Sopel to request a capability when it starts.
        By prefixing the capability with `-`, it will be ensured that the
        capability is not enabled. Simmilarly, by prefixing the capability with
        `=`, it will be ensured that the capability is enabled. Requiring and
        disabling is "first come, first served"; if one module requires a
        capability, and another prohibits it, this function will raise an
        exception in whichever module loads second. An exception will also be
        raised if the module is being loaded after the bot has already started,
        and the request would change the set of enabled capabilities.
        If the capability is not prefixed, and no other module prohibits it, it
        will be requested.  Otherwise, it will not be requested. Since
        capability requests that are not mandatory may be rejected by the
        server, as well as by other modules, a module which makes such a
        request should account for that possibility.
        The actual capability request to the server is handled after the
        completion of this function. In the event that the server denies a
        request, the `failure_callback` function will be called, if provided.
        The arguments will be a `Sopel` object, and the capability which was
        rejected. This can be used to disable callables which rely on the
        capability."""
        #TODO raise better exceptions
        cap = capability[1:]
        prefix = capability[0]

        if prefix == '-':
            if self.connection_registered and cap in self.enabled_capabilities:
                raise Exception('Can not change capabilities after server '
                                'connection has been completed.')
            entry = self._cap_reqs.get(cap, [])
            if any((ent[0] != '-' for ent in entry)):
                raise Exception('Capability conflict')
            entry.append((prefix, module_name, failure_callback))
            self._cap_reqs[cap] = entry
        else:
            if prefix != '=':
                cap = capability
                prefix = ''
            if self.connection_registered and (cap not in
                                               self.enabled_capabilities):
                raise Exception('Can not change capabilities after server '
                                'connection has been completed.')
            entry = self._cap_reqs.get(cap, [])
            if any((ent[0] == '-' for ent in entry)):
                raise Exception('Capability conflict')
            entry.append((prefix, module_name, failure_callback))
            self._cap_reqs[cap] = entry

if __name__ == '__main__':
    print __doc__