#!/usr/bin/env python
import argparse, new, os, sys, time, traceback
from collections import defaultdict

from twisted.words.protocols import irc
from twisted.internet import reactor, protocol, defer
from twisted.python import log

import yaml

### config ###
CONFIG_FILE = 'bot.config'

class Config(dict):
    """Handles all configuration for chii. Reads/writes from/to YAML.
       Acts like a normal dict, except returns default value or None
       for non-existant keys."""

    defaults = {
        'nickname': 'chii',
        'realname': 'chii',
        'server': 'irc.esper.net',
        'port': 6667,
        'channels': ['chiisadventure'],
        'cmd_prefix': '.',
        'modules': ['commands', 'events', 'tasks'],
        'owner': 'zk!is@whatit.is',
        'user_roles': {'admins': ['zk!is@whatit.is']},
        'log_dir': 'logs',
        'log_channel': True,
        'log_chii': False,
        'log_stdout': True,
    }

    def __init__(self, file):
        self.file = file
        if os.path.isfile(file):
            with open(file) as f:
                config = yaml.load(f.read())
                for k in config:
                    self.__setitem__(k, config[k])

    def __getitem__(self, key):
        if self.__contains__(key):
            return super(Config, self).__getitem__(key)
        elif key in self.defaults:
            return self.defaults[key]

    def save(self):
        f = open(self.file, 'w')
        f.write(yaml.dump(sorted(self), default_flow_style=False))
        f.close()

    def save_defaults(self):
        f = open(self.file, 'w')
        f.write(yaml.dump(sorted(self.defaults), default_flow_style=False))
        f.close()

# get config
config = Config(CONFIG_FILE)

### decorators ###
def command(*args, **kwargs):
    """Decorator which adds callable to command registry"""
    if args and not hasattr(args[0], '__call__') or kwargs:
        # decorator used with args for aliases or keyward arg
        def decorator(func):
            def wrapper(*func_args, **func_kwargs):
                return func(*func_args, **func_kwargs)
            wrapper._registry = 'commands'
            wrapper.__name__ = func.__name__
            wrapper.__doc__ = func.__doc__
            if args:
                wrapper._command_names = args
            else:
                # used with keyword arg but not alias names!
                wrapper._command_names = (func.__name__,)
            if 'restrict' in kwargs:
                wrapper._restrict = kwargs['restrict']
            else:
                wrapper._restrict = None
            return wrapper
        return decorator
    else:
        # used without any args
        func = args[0]
        def wrapper(*func_args, **func_kwargs):
            return func(*func_args, **func_kwargs)
        wrapper._registry = 'commands'
        wrapper.__name__ = func.__name__
        wrapper.__doc__ = func.__doc__
        wrapper._command_names = (func.__name__,)
        if 'restrict' in kwargs:
            wrapper._restrict = kwargs['restrict']
        else:
            wrapper._restrict = None
        return wrapper

def event(event_type):
    """Decorator which adds callable to the event registry"""
    def decorator(func):
        def wrapper(*func_args, **func_kwargs):
            return func(*func_args, **func_kwargs)
        wrapper._registry = 'events'
        wrapper._event_type = event_type
        wrapper.__name__ = func.__name__
        wrapper.__doc__ = func.__doc__
        return wrapper
    return decorator

def task(*args):
    """Decorator which adds callable to task registry"""
    def decorator(func):
        def wrapper(*func_args, **func_kwargs):
            return func(*func_args, **func_kwargs)
        wrapper.__name__ = func.__name__
        wrapper.__doc__ = func.__doc__
        wrapper._registry = 'tasks'
        wrapper._task_interval = args[0]
        return wrapper
    return decorator

### application logic ###
class Logger:
    """Logs both irc events and chii events into different log files"""
    def __init__(self, logs_dir, channels, nickname):
        if os.path.isdir(logs_dir):
            if channels:
                self.channels = dict(((channel, open(os.path.join(logs_dir, channel), 'a')) for channel in channels))
            else:
                self.channels = {}
                self.log = lambda *args: None
            if nickname:
                self.bot_log = open(os.path.join(logs_dir, nickname), 'a')
                self.observer = log.FileLogObserver(self.bot_log)
                self.observer.start()
            else:
                self.nickname = None

    def log(self, message, channel=None):
        """Write a message to the file."""
        if channel:
            logfiles = [self.channels['channel']]
        else:
            logfiles = self.channels.values()
        for file in logfiles:
            timestamp = time.strftime("[%H:%M:%S]", time.localtime(time.time()))
            file.write('%s %s\n' % (timestamp, message))
            file.flush()

    def close(self):
        if self.nickname:
            self.observer.stop()
            self.bot_log.close()
        for channel in self.channels:
            self.channels[channel].close()

class Chii:
    """Application logic for our chiibot"""
    def _add_to_registry(self, mod):
        """Adds registred methods to registry"""
        def add_command(method):
            for name in method._command_names:
                if name in self.commands:
                    print 'Warning! commands registry already contains %s' % name
                self.commands[name] = new.instancemethod(method, self, Chii)
    
        def add_event(method):
            self.events[method._event_type].append(new.instancemethod(method, self, Chii))
    
        def add_task(method):
            self.tasks.append(new.instancemethod(method, self, Chii))

        dispatch = {'commands': add_command, 'events': add_event, 'tasks': add_task}

        registered = filter(lambda x: hasattr(x, '_registry'), (getattr(mod, x) for x in dir(mod) if not x.startswith('_')))
        for method in registered:
            dispatch.get(method._registry)(method)

    def _import_module(self, package, module):
        """imports, reloading if neccessary given package.module"""
        path = '%s.%s' % (package, module)

        # cleanup if we're reloading
        if path in sys.modules:
            print 'Reloading', path
            mod = sys.modules[path]
            for attr in filter(lambda x: x != '__name__', dir(mod)):
                delattr(mod, attr)
            reload(mod)
        else:
            print 'Importing', path

        # try to import module
        try:
            mod = __import__(path, globals(), locals(), [module], -1)
            return mod
        except Exception as e:
            print 'Error importing %s: %s' % (path, e)
            traceback.print_exc()

    def _update_registry(self):
        """Updates command, event task registries"""
        paths = self.config['modules']
        if paths:
            self.commands = {}
            self.events = defaultdict(list)
            self.tasks = []
            for path in paths:
                path = os.path.abspath(path)
                package = os.path.split(path)[-1]
                modules = [f.replace('.py', '') for f in os.listdir(path) if f.endswith('.py') and f != '__init__.py']
                for module in modules:
                    mod = self._import_module(package, module)
                    if mod:
                        self._add_to_registry(mod)
            print '[commands]', ', '.join(sorted(x for x in self.commands))
            print '[events]', ': '.join(sorted(x + ': ' + ', '.join(sorted(y.__name__ for y in self.events[x])) for x in self.events))
            print '[tasks]', ', '.join(sorted(x for x in self.tasks))

    def _handle_command(self, nick, host, channel, msg):
        """Handles commands, passing them proper args, etc"""
        msg = msg.split()
        command, args = msg[0][1:].lower(), []
        command = self.commands.get(command, None)
        if command:
            if check_permission(command._restrict, nick, host):
                if len(msg) > 1:
                    args = msg[1:]
                try:
                    response = command(nick, host, channel, *args)
                except Exception as e:
                    response = 'ur shit am fuked! %s' % e
                    traceback.print_exc()
                if response:
                    if channel == self.nickname:
                        self.msg(nick, response)
                    else:
                        self.msg(channel, response)
                    self.logger.log("<%s> %s" % (self.nickname, response))


    def _handle_event(self, event_type=None, respond=False, *args):
        """Handles event and catches errors, returns result of event as bot message"""
        events = self.events.get(event_type, None)
        if events:
            for event in events:
                try:
                    response = event(*args)
                except Exception as e:
                    response = 'ur shit am fuked! %s' % e
                    traceback.print_exc()
                if respond and response:
                    # only return something if this event is caught in a channel
                    self.msg(respond, response)
                    self.logger.log("<%s> %s" % (self.nickname, response))

    def check_permission(self, restrict_to, nick, host):
        if restrict_to is None:
            return True
        for member in (nick, host, '!'.join((nick, host))):
            if member in self.config['user_roles'][restrict_to]:
                return True
        return False


### twisted protocol/factory ###
class ChiiBot(irc.IRCClient, Chii):
    config = config
    nickname = config['nickname']
    realname = config['realname']

    # setup logging
    log_args = [None, None, None]
    if config['log_dir']:
        log_args[0] = config['log_dir']
        if config['log_channel']:
            log_args[1] = config['channels']
        if config['log_chii']:
            log_args[2] = nickname
    logger = Logger(*log_args)

    def connectionMade(self):
        irc.IRCClient.connectionMade(self)
        self.logger.log("[connected at %s]" % time.asctime(time.localtime(time.time())))
        self._update_registry()
        self._handle_event('load', False)

    def connectionLost(self, reason):
        irc.IRCClient.connectionLost(self, reason)
        self.logger.log("[disconnected at %s]" % time.asctime(time.localtime(time.time())))
        self.logger.close()

    def signedOn(self):
        """Called when bot has succesfully signed on to server."""
        self.setNick(self.nickname)
        if self.config['identpass']:
            self.msg('nickserv', 'identify %s' % self.config['identpass'])
        for channel in self.config['channels']:
            self.join(channel)

    def joined(self, channel):
        """This will get called when the bot joins a channel."""
        self.logger.log("[I have joined %s]" % channel)

    def privmsg(self, user, channel, msg):
        """This will get called when the bot receives a message."""
        nick, host = user.split('!')
        self.logger.log("<%s> %s" % (nick, msg))

        # handle message events
        self._handle_event('msg', channel, nick, host, channel, msg)
        if channel == self.nickname:
            self._handle_event('pubmsg', channel, nick, host, channel, msg)
        else:
            self._handle_event('pubmsg', channel, nick, host, channel, msg)

        # Check if we're getting a command
        if msg.startswith(self.config['cmd_prefix']):
            self._handle_command(nick, host, channel, msg)

    def action(self, user, channel, msg):
        """This will get called when the bot sees someone do an action."""
        nick, host = user.split('!')
        self._handle_event('action', channel, nick, host, channel, msg)
        self.logger.log("* %s %s" % (nick, msg))

    def userJoined(self, user, channel):
        """Called when I see another user joining a channel."""
        pass

    def userLeft(self, user, channel):
        """Called when I see another user leaving a channel."""
        pass

    def userQuit(self, user, quitMessage):
        """Called when I see another user disconnect from the network."""
        pass

    def userKicked(self, kickee, channel, kicker, message):
        """Called when I observe someone else being kicked from a channel."""
        pass

    # irc callbacks
    def irc_NICK(self, prefix, params):
        """Called when an IRC user changes their nickname."""
        old_nick = prefix.split('!')[0]
        new_nick = params[0]
        self.logger.log("%s is now known as %s" % (old_nick, new_nick))

    def alterCollidedNick(self, nickname):
        """
        Generate an altered version of a nickname that caused a collision in an
        effort to create an unused related name for subsequent registration.
        """
        return nickname + '`'

    def irc_ERR_NICKNAMEINUSE(self, prefix, params):
        """
        Called when we try to register or change to a nickname that is already
        taken.
        """
        if self.config['identpass']:
            self.msg('nickserv', 'ghost %s %s' % (self.nickname, self.config['identpass']))
            self.setNick(self.nickname)
        else:
            self.setNick(self.alterCollidedNick(self._attemptedNick))

class ChiiFactory(protocol.ClientFactory):
    """A factory for ChiiBots."""
    protocol = ChiiBot

    def clientConnectionLost(self, connector, reason):
        """If we get disconnected, reconnect to server."""
        connector.connect()

    def clientConnectionFailed(self, connector, reason):
        print "connection failed:", reason
        reactor.stop()

### main ###
if __name__ == '__main__':
    # no config? DIE
    if not config:
        print 'No config file found! Create', CONFIG_FILE
        sys.exit(1)

    # initialize logging
    log.startLogging(sys.stdout)
    
    # create factory protocol and application
    factory = ChiiFactory()

    # connect factory to this host and port
    if config['ssl']:
        from twisted.internet import ssl
        contextFactory = ssl.ClientContextFactory()
        reactor.connectSSL(config['server'], config['port'], factory, contextFactory)
    else:
        reactor.connectTCP(config['server'], config['port'], factory)

    # run bot
    reactor.run()
