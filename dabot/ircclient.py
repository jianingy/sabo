# -*- mode: python -*-

# -*- python -*-

"""
return value format:
{
  "text": "text to say",
  "channels": ["channel1", "channel2"]
  "users": ["user1", "user2"]
}
"""

from twisted.internet import reactor, protocol, threads, defer
from twisted.words.protocols import irc
from twisted.web.client import getPage
from twisted.python.failure import Failure
from twisted.python import log
from dabot.setting import ConfigError
from cjson import encode as json_encode, decode as json_decode
from logging import WARN, DEBUG

import re
import sys
import time

__all__ = ['IRCClient', 'IRCClientFactory', 'ConfigError']


class IRCClient(irc.IRCClient):

    EXPAND_RE = re.compile("%{(\w+)}")

    def __init__(self, factory, servername):
        from dabot.setting import setting
        self.factory = factory
        log.msg("IRCClient initialized", level=DEBUG)
        try:
            self.servername = servername
            self.server = setting["servers"][self.servername]
            self.siblings = self.factory.siblings
            self.encodings = setting["encodings"]
            self.default_encoding = setting["servers"].get("encoding", "UTF-8")
            self.nickname = setting["profile"]["nick"]
            self.realname = setting["profile"].get("real", "dabot")
            self.versionName = setting["profile"].get("version_name", "dabot")
            self.versionNum = setting["profile"].get("version_num", "1.0")
            self.password = setting["profile"].get("password", None)
            self.channels = setting["channels"]
            self.handlers = setting["handlers"]
        except Exception as e:
            raise ConfigError("malformed configuration: %s" % str(e))

        self.context = dict(setting=setting)
        self._mq = list()

        # synchronous notification
        # json = urllib.encode(setting)
        # urllib2.urlopen(_api, json)

    ##########################################################################
    # Basic Functions
    ##########################################################################

    def _reload(self):
        """reload reloadable settings :)"""
        from dabot.setting import setting
        try:
            self.server = setting["servers"][self.servername]
            self.encodings = setting["encodings"]
            self.default_encoding = setting["servers"].get("encoding", "UTF-8")
            self.channels = setting["channels"]
            self.handlers = setting["handlers"]
            self.signedOn()
            self.schedule()
        except Exception as e:
            raise ConfigError("malformed configuration: %s" % str(e))

    def _match_encoding(self, channel):
        for e in self.encodings:
            if ("match_server" in e and
                not e["match_server"].match(self.servername)):
                continue

            if ("match_channel" in e and
                not e["match_channel"].match(channel)):
                continue

            return e["encoding"]

        return self.default_encoding

    def _decode(self, channel, msg):
        index = (self.servername, channel)
        if index in self.channels:
            return msg.decode(self.channels[index]["encoding"], "ignore")
        else:
            return msg.decode(self._match_encoding(channel), "ignore")

    def _encode(self, channel, msg):
        index = (self.servername, channel)
        if index in self.channels:
            return msg.encode(self.channels[index]["encoding"], "ignore")
        else:
            return msg.encode(self._match_encoding(channel), "ignore")

    def _complain(self, err):
        log.msg(str(err), level=WARN)

    def _expandvar(self, text, vars):

        def __expand(m):
            name = m.group(1)
            print name, "X" * 80
            if name in vars:
                return vars[name]
            else:
                return m.group(0)

        return self.EXPAND_RE.subn(__expand, text)[0]

    ##########################################################################
    # Message Queue Manipulation
    ##########################################################################

    def mq_append(self, data):
        log.msg("mq_append[%s]: %s" % (self.servername, str(data)),
                level=DEBUG)
        self._mq.append(data)

    def _send_text(self, message):
        if "channels" in message and isinstance(message["channels"], list):
            for channel in message["channels"]:
                text = self._encode(channel, message["text"])
                if isinstance(channel, unicode):
                    channel = self._encode(channel, channel)
                self.msg(channel, text)

        if "users" in message and isinstance(message["users"], list):
            for user in message["users"]:
                text = self._encode(user, message["text"])
                if isinstance(user, unicode):
                    user = self._encode(user, user)
                self.msg(user, text)

    def _send(self, message):
        if "text" in message:
            return self._send_text(message)

    @defer.inlineCallbacks
    def schedule(self):
        while self._mq:
            message = self._mq.pop()
            yield threads.deferToThread(self._send, message)

    ##########################################################################
    # handler infrastructure
    ##########################################################################

    def _match(self, h, servername, user, channel, text):

        if "match_server" in h and not h["match_server"].match(servername):
            log.msg("server not match: " + servername, level=DEBUG)
            return False

        if "match_channel" in h and not h["match_channel"].match(channel):
            log.msg("channel not match: " + channel, level=DEBUG)
            return False

        if "match_user" in h and not h["match_user"].match(user):
            log.msg("user not match: " + user, level=DEBUG)
            return False

        # use UTF-8 since regex in yaml are UTF-8
        text = text.encode("UTF-8")
        if "match_text" in h and not h["match_text"].match(text):
            log.msg("text not match: " + text, level=DEBUG)
            return False

        return True

    @defer.inlineCallbacks
    def _handled(self, value):

        if isinstance(value, Failure):
            self._complain(str(value.value))
            return

        self.mq_append(value)
        retval = yield self.schedule()
        defer.returnValue(retval)

    def _default_target(self, user, channel):
        if user.index("!") > -1:
            user = user.split("!")[0]

        if channel == self.nickname:
            return ([user], [])
        else:
            return ([], [channel])

    def _http_done(self, message, user, channel):
        message = json_decode(message)
        if "users" not in message and "channels" not in message:
            message["users"], message["channels"] = \
              self._default_target(user, channel)
        return message

    def _execute_builtin(self, h, user, channel, text):
        if h["builtin"] == "reload":
            users, channels = self._default_target(user, channel)
            from dabot.setting import reload
            try:
                reload()
                self._reload()
                return dict(users=users, channels=channels,
                            text="reloaded successfully")
            except:
                return dict(users=users, channels=channels,
                            text="failed!")

    def _dispatch(self, h, user, channel, text=""):

        if "text" in h:
            users, channels = self._default_target(user, channel)
            reply = dict(users=users, channels=channels, text=h["text"])
            d = defer.succeed(reply)
            d.addBoth(self._handled)

        if "http" in h:
            postdata = json_encode(dict(user=user,
                                        channel=channel,
                                        text=text))
            d = getPage(h["http"], method="POST", postdata=postdata)
            d.addCallback(self._http_done, user, channel)
            d.addBoth(self._handled)

        if "builtin" in h:
            d = threads.deferToThread(self._execute_builtin, h,
                                      user, channel, text)
            d.addBoth(self._handled)

        if "redirect" in h:
            if user.index("!") > -1:
                user = user.split("!")[0]
            items = map(lambda x: x.split("/", 2), h["redirect"])
            local_channels, remote_channels = list(), dict()
            if "prefix" in h:
                ctx = dict(user=user, channel=channel,
                           servername=self.servername)
                prefix = self._expandvar(h["prefix"], ctx)
            else:
                prefix = u"%s@%s/%s" % (unicode(user), unicode(self.servername), unicode(channel))
            for servername, rchannel in items:
                log.msg("%s:%s/%s -> %s" % (servername, channel, user, rchannel), level=DEBUG)
                if servername == self.servername:
                    if rchannel == user:
                        # prevents send the same message to the sender
                        continue
                    local_channels.append(rchannel)
                elif servername in self.siblings:
                    if servername not in remote_channels:
                        remote_channels[servername] = [rchannel]
                    else:
                        remote_channels[servername].append(rchannel)

            # redirect local messages
            reply = dict(channels=local_channels, text="%s: %s" % (prefix,text))
            d = defer.succeed(reply)
            d.addBoth(self._handled)

            # redirect remote messages
            for servername, channels in remote_channels.items():
                reply = dict(channels=channels, text=u"%s: %s" % (prefix, text))
                if servername in self.siblings:
                    p = self.siblings[servername].protocol
                    p.mq_append(reply)
                    p.schedule()

    def lineReceived(self, line):
        log.msg(">> %s" % str(line), level=DEBUG)
        sys.stdout.flush()
        irc.IRCClient.lineReceived(self, line)

    def sendLine(self, line):
        log.msg("<< %s" % str(line), level=DEBUG)
        irc.IRCClient.sendLine(self, line)

    ##########################################################################
    # Protocol event dealers
    ##########################################################################

    def connectionMade(self):
        self.factory.reconnect_delay = 1
        return irc.IRCClient.connectionMade(self)

    def signedOn(self):
        channels = filter(lambda x: x[0] == self.servername,
                          self.channels.keys())
        log.msg("join in %s" % channels)
        return map(lambda x: self.join(x[1]), channels)

    def _privmsg(self, user, channel, msg):
        text = self._decode(channel, msg)

        def __match(h):
            return self._match(h, self.servername, user, channel, text)

        for h in filter(__match, self.handlers["privmsg"]):
            self._dispatch(h, user, channel, text)

    def privmsg(self, user, channel, msg):
        d = threads.deferToThread(self._privmsg, user, channel, msg)
        d.addErrback(self._complain)

    def _userJoined(self, user, channel):

        def __match(h):
            return self._match(h, self.servername, user, channel, "")

        for h in filter(__match, self.handlers["user_joined"]):
            self._dispatch(h, user, channel)

    def userJoined(self, user, channel):
        d = threads.deferToThread(self._userJoined, user, channel)
        d.addErrback(self._complain)

    def _joined(self, channel):
        servername = self.servername

        def __match(h):
            if "match_server" in h and not h["match_server"].match(servername):
                return False

            if "match_channel" in h and not h["match_channel"].match(channel):
                return False

            return True

        for h in filter(__match, self.handlers["joined"]):
            self._dispatch(h, self.nickname, channel)

    def joined(self, channel):
        d = threads.deferToThread(self._joined, channel)
        d.addErrback(self._complain)
"""
    def irc_RPL_NAMREPLY(self, prefix, params):
        irc.IRCClient.irc_RPL_NAMEREPLY(self, prefix, params)

    def irc_RPL_ENDOFNAMES(self, prefix, params):
        irc.IRCClient.irc_ENDOFNAMES(self, prefix, params)

    def irc_NICK(self, prefix, params):
        irc.IRCClient.irc_NICK(self, prefix, params)
"""


class IRCClientFactory(protocol.ClientFactory):

    def buildProtocol(self, addr):
        self.protocol = IRCClient(self, self.servername)
        self.reconnect_delay = 1
        return self.protocol

    def __init__(self, servername, siblings):
        from dabot.setting import setting
        self.servername = servername
        self.siblings = siblings
        server = setting["servers"][self.servername]
        self.host = server["host"]
        self.port = server["port"]
        self.protocol = None

    def reconnect(self, connector, reason):
        self.reconnect_delay = self.reconnect_delay * 2
        time.sleep(self.reconnect_delay)
        connector.connect()

    def startedConnecting(self, connector):
        log.msg("connecting to %s" % connector, level=DEBUG)

    def clientConnectionLost(self, connector, reason):
        """If we lost server, reconnect to it"""
        log.msg("connection lost. start reconnecting", level=WARN)
        self.reconnect(connector, reason)

    def clientConnectFailed(self, connector, reason):
        log.msg("connection failed:" + reason, level=WARN)
        self.reconnect(connector, reason)
