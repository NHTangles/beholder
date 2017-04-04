"""
deathbot.py - a game-reporting IRC bot for AceHack
Copyright (c) 2011, Edoardo Spadolini
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are
met:

1. Redistributions of source code must retain the above copyright
notice, this list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright
notice, this list of conditions and the following disclaimer in the
documentation and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS
IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED
TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A
PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED
TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""

from twisted.internet import reactor, protocol, ssl, task
from twisted.words.protocols import irc
from twisted.python import filepath
from twisted.application import internet, service
import datetime # for timestamp stuff - Not used?
import time     # for !time
import ast      # for conduct/achievement bitfields - not really used
import os       # for check path exists (dumplogs)
import re       # for hello.
import urllib   # for dealing with NH4 variants' #&$#@ spaces in filenames.

TEST= False
#TEST = True  # uncomment for testing

# fn
HOST, PORT = "chat.us.freenode.net", 6697
CHANNEL = "#hardfought"
NICK = "Beholder"
if TEST:
    CHANNEL = "#hftest"
    NICK = "BeerHolder"

def fromtimestamp_int(s):
    return datetime.datetime.fromtimestamp(int(s))

def timedelta_int(s):
    return datetime.timedelta(seconds=int(s))

def isodate(s):
    return datetime.datetime.strptime(s, "%Y%m%d").date()

xlogfile_parse = dict.fromkeys(
    ("points", "deathdnum", "deathlev", "maxlvl", "hp", "maxhp", "deaths",
     "uid", "turns", "xplevel", "exp"), int)
xlogfile_parse.update(dict.fromkeys(
    ("conduct", "event", "carried", "flags", "achieve"), ast.literal_eval))
#xlogfile_parse["starttime"] = fromtimestamp_int
#xlogfile_parse["curtime"] = fromtimestamp_int
#xlogfile_parse["endtime"] = fromtimestamp_int
#xlogfile_parse["realtime"] = timedelta_int
#xlogfile_parse["deathdate"] = xlogfile_parse["birthdate"] = isodate

def parse_xlogfile_line(line, delim):
    record = {}
    for field in line.strip().split(delim):
        key, _, value = field.partition("=")
        if key in xlogfile_parse:
            value = xlogfile_parse[key](value)
        record[key] = value
    return record

#def xlogfile_entries(fp):
#    if fp is None: return
#    with fp.open("rt") as handle:
#        for line in handle:
#            yield parse_xlogfile_line(line)

class DeathBotProtocol(irc.IRCClient):
    nickname = NICK
    username = "beholder"
    realname = "Beholder"
    try:
        password = open("/opt/beholder/pw", "r").read().strip()
    except:
        pass
    if TEST: password = "NotTHEPassword"

    sourceURL = "https://ascension.run/deathbot.py"
    versionName = "deathbot.py"
    versionNum = "0.2"

    dump_url_prefix = "https://hardfought.org/userdata/{name[0]}/{name}/"
    dump_file_prefix = "/opt/nethack/hardfought.org/dgldir/userdata/{name[0]}/{name}/"

    xlogfiles = {filepath.FilePath("/opt/nethack/hardfought.org/nh343/var/xlogfile"): ("nh", ":", "nh343/dumplog/{starttime}.nh343.txt"),
                 filepath.FilePath("/opt/nethack/hardfought.org/nhdev/var/xlogfile"): ("nd", "\t", "nhdev/dumplog/{starttime}.nhdev.txt"),
                 filepath.FilePath("/opt/nethack/hardfought.org/gh/var/xlogfile"): ("gh", ":", "gh/dumplog/{starttime}.gh.txt"),
                 filepath.FilePath("/opt/nethack/hardfought.org/fiqhackdir/data/xlogfile"): ("fh", ":", "fiqhack/dumplog/{dumplog}"),
                 filepath.FilePath("/opt/nethack/hardfought.org/un531/var/unnethack/xlogfile"): ("un", ":", "un531/dumplog/{starttime}.un531.txt.html")}
    livelogs  = {filepath.FilePath("/opt/nethack/hardfought.org/nh343/var/livelog"): ("nh", ":"),
                 filepath.FilePath("/opt/nethack/hardfought.org/nhdev/var/livelog"): ("nd", "\t"),
                 filepath.FilePath("/opt/nethack/hardfought.org/gh/var/livelog"): ("gh", ":"),
                 filepath.FilePath("/opt/nethack/hardfought.org/un531/var/unnethack/livelog"): ("un", ":")}

    looping_calls = None

    def signedOn(self):
        self.factory.resetDelay()
        self.startHeartbeat()
        self.join(CHANNEL)

        self.logs = {}
        for xlogfile, (variant, delim, dumpfmt) in self.xlogfiles.iteritems():
            self.logs[xlogfile] = (self.xlogfileReport, variant, delim, dumpfmt)
        for livelog, (variant, delim) in self.livelogs.iteritems():
            self.logs[livelog] = (self.livelogReport, variant, delim, "")

        self.logs_seek = {}
        self.looping_calls = {}

        #lastgame shite
        self.lastgame = "No last game recorded"
        self.lg = {}
        self.lastasc = "No last ascension recorded"
        self.la = {}
        # for populating lg/la per player at boot, we need to track game end times
        # variant and variant:player don't need this if we assume the xlogfiles are
        # ordered within variant.
        self.lge = {}
        self.tlastgame = 0
        self.lae = {}
        self.tlastasc = 0

        # for !tell
        self.tellbuf = {}

        self.commands = {"ping"     : self.doPing,
                         "time"     : self.doTime,
                         "hello"    : self.doHello,
                         "beer"     : self.doBeer,
                         "tell"     : self.takeMessage,
                         "lastgame" : self.lastGame,
                         "lastasc"  : self.lastAsc}

        # seek to end of livelogs
        for filepath in self.livelogs:
            with filepath.open("r") as handle:
                handle.seek(0, 2)
                self.logs_seek[filepath] = handle.tell()

        # sequentially read xlogfiles from beginning to pre-populate lastgame data.
        for filepath in self.xlogfiles:
            with filepath.open("r") as handle:
                for line in handle:
                    delim = self.logs[filepath][2]
                    game = parse_xlogfile_line(line, delim)
                    game["variant"] = self.logs[filepath][1]
                    game["dumpfmt"] = self.logs[filepath][3]
                    for line in self.logs[filepath][0](game,False):
                        pass
                self.logs_seek[filepath] = handle.tell()

        for filepath in self.logs:
            self.looping_calls[filepath] = task.LoopingCall(self.logReport, filepath)
            self.looping_calls[filepath].start(3)

        # Additionally, keep an eye on our nick to make sure it's right.
        # Perhaps we only need to set this up if the nick was originally
        # in use when we signed on, but a 30-second looping call won't kill us
        self.looping_calls["nick"] = task.LoopingCall(self.nickCheck)
        self.looping_calls["nick"].start(30)

    def nickCheck(self):
        if (self.nickname != NICK):
            self.setNick(NICK)

    def nickChanged(self, nn):
        # catch successful changing of nick from above and identify with nickserv
        if TEST: self.msg("Tangles", "identify " + nn + " " + self.password)
        else: self.msg("NickServ", "identify " + nn + " " + self.password)


    # implement commands here
    def doPing(self, sender, replyto, msgwords):
        self.msg(replyto, sender + ": Pong! " + " ".join(msgwords[1:]))

    def doTime(self, sender, replyto, msgwords):
        self.msg(replyto, sender + ": " + time.strftime("%c %Z"))

    def doHello(self, sender, replyto, msgwords = 0):
        self.msg(replyto, "Hello " + sender + ", Welcome to " + CHANNEL)

    def doBeer(self, sender, replyto, msgwords):
        self.msg(replyto, sender + ": It's your shout!")

    def takeMessage(self, sender, replyto, msgwords):
        rcpt = msgwords[1]
        if (replyto == sender): #this was a privmsg
            forwardto = rcpt # so we pass a privmsg
        else: # !tell on channel
            forwardto = replyto # so pass to channel
        if not self.tellbuf.get(rcpt,False):
            self.tellbuf[rcpt] = []
        self.tellbuf[rcpt].append((forwardto,sender," ".join(msgwords[2:])))
        self.msg(replyto,"Will do, " + sender + "!")

    def checkMessages(self, user):
        # this runs every time someone speaks on the channel,
        # so return quickly if there's nothing to do
        if not self.tellbuf.get(user,False): return
        for (forwardto,sender,message) in self.tellbuf[user]:
            self.msg(forwardto, user + ": Message from " + sender + ": " + message)
        self.tellbuf[user] = []

    def lastGame(self, sender, replyto, msgwords):
        if (len(msgwords) >= 3): #var, plr, any order.
            dl = self.lg.get(":".join(msgwords[1:3]).lower(), False)
            if not dl:
                dl = self.lg.get(":".join(msgwords[2:0:-1]).lower(),
                                 "No last game for (" + ",".join(msgwords[1:3]) + ")")
            self.msg(replyto, sender + ": " + dl)
            return
        if (len(msgwords) == 2): #var OR plr - don't care which
            dl = self.lg.get(msgwords[1].lower(),"No last game for " + msgwords[1])
            self.msg(replyto, sender + ": " + dl)
            return
        self.msg(replyto, sender + ": " + self.lastgame)

    def lastAsc(self, sender, replyto, msgwords):
        if (len(msgwords) >= 3): #var, plr, any order.
            dl = self.la.get(":".join(msgwords[1:3]).lower(),False)
            if (dl == False):
                dl = self.la.get(":".join(msgwords[2:0:-1]).lower(),
                                 "No last ascension for (" + ",".join(msgwords[1:3]) + ")")
            self.msg(replyto, sender + ": " + dl)
            return
        if (len(msgwords) == 2): #var OR plr - don't care which
            dl = self.la.get(msgwords[1].lower(),"No last ascension for " + msgwords[1])
            self.msg(replyto, sender + ": " + dl)
            return
        self.msg(replyto, sender + ": " + self.lastasc)

    # Listen to the chatter
    def privmsg(self, sender, dest, message):
        sender = sender.partition("!")[0]
        if (dest == CHANNEL): #public message
            replyto = CHANNEL
        else: #private msg
            replyto = sender
        # Hello processing first.
        if re.match(r'^(hello|hi|hey)[!?. ]*$', message.lower()):
            self.doHello(sender, replyto)
        # Message checks next.
        self.checkMessages(sender)
        # ignore other channel noise
        if (message[0] != '!'):
            if (dest == CHANNEL): return
        else: # pop the '!'
            message = message[1:]
        msgwords = message.split(" ")
        if self.commands.get(msgwords[0].lower(), False):
            self.commands[msgwords[0].lower()](sender, replyto, msgwords)


    def startscummed(self, game):
        return game["death"] in ("quit", "escaped") and game["points"] < 1000

    def xlogfileReport(self, game, report = True):
        if self.startscummed(game): return

        # Need to figure out the dump path before messing with the name below
        dumpfile = (self.dump_file_prefix + game["dumpfmt"]).format(**game)
        dumpurl = "(sorry, no dump exists for {variant}:{name})".format(**game)
        if os.path.exists(dumpfile) or TEST: # dump files may not exist on test system
            # quote only the game-specific part, not the prefix.
            # Otherwise it qutes the : in https://
            # assume the rest of the url prefix is safe.
            dumpurl = urllib.quote(game["dumpfmt"].format(**game))
            dumpurl = self.dump_url_prefix.format(**game) + dumpurl
        self.lg["{variant}:{name}".format(**game).lower()] = dumpurl
        if (game["endtime"] > self.lge.get(game["name"].lower(), 0)):
            self.lge[game["name"].lower()] = game["endtime"]
            self.lg[game["name"].lower()] = dumpurl
        self.lg[game["variant"].lower()] = dumpurl
        if (game["endtime"] > self.tlastgame):
            self.lastgame = dumpurl
            self.tlastgame = game["endtime"]
        if game["death"] in ("ascended"):
            game["ascsuff"] = "\n" + dumpurl
            self.la["{variant}:{name}".format(**game).lower()] = dumpurl
            if (game["endtime"] > self.lae.get(game["name"].lower(), 0)):
                self.lae[game["name"].lower()] = game["endtime"]
                self.la[game["name"].lower()] = dumpurl
            self.la[game["variant"].lower()] = dumpurl
            if (game["endtime"] > self.tlastasc):
                self.lastasc = dumpurl
                self.tlastasc = game["endtime"]
        else:
            game["ascsuff"] = ""

        if (not report): return

        if game.get("charname", False):
            if game.get("name", False):
                if game["name"] != game["charname"]:
                    game["name"] = "{charname} ({name})".format(**game)
            else:
                game["name"] = game["charname"]

        if game.get("while", False) and game["while"] != "":
            game["death"] += (", while " + game["while"])

        if (game.get("mode", "normal") == "normal" and
              game.get("modes", "normal") == "normal"):
            yield ("[{variant}] {name} ({role} {race} {gender} {align}), "
                   "{points} points, T:{turns}, {death}{ascsuff}").format(**game)
        else:
            if "modes" in game:
                if game["modes"].startswith("normal,"):
                    game["mode"] = game["modes"][7:]
                else:
                    game["mode"] = game["modes"]
            yield ("[{variant}] {name} ({role} {race} {gender} {align}), "
                   "{points} points, T:{turns}, {death}, "
                   "in {mode} mode{ascsuff}").format(**game)

    def livelogReport(self, event):
        if event.get("charname", False):
            if event.get("player", False):
                if event["player"] != event["charname"]:
                    event["player"] = "{charname} ({player})".format(**event)
            else:
                event["player"] = event["charname"]

        if "historic_event" in event and "message" not in event:
            if event["historic_event"].endswith("."):
                event["historic_event"] = event["historic_event"][:-1]
            event["message"] = event["historic_event"]

        if "message" in event:
            yield ("[{variant}] {player} ({role} {race} {gender} {align}) "
                   "{message}, on T:{turns}").format(**event)
        elif "wish" in event:
            yield ("[{variant}] {player} ({role} {race} {gender} {align}) "
                   'wished for "{wish}", on T:{turns}').format(**event)
        elif "shout" in event:
            yield ("[{variant}] {player} ({role} {race} {gender} {align}) "
                   'shouted "{shout}", on T:{turns}').format(**event)
        elif "bones_killed" in event:
            yield ("[{variant}] {player} ({role} {race} {gender} {align}) "
                   "killed the {bones_monst} of {bones_killed}, "
                   "the former {bones_rank}, on T:{turns}").format(**event)
        elif "killed_uniq" in event:
            yield ("[{variant}] {player} ({role} {race} {gender} {align}) "
                   "killed {killed_uniq}, on T:{turns}").format(**event)

    def connectionLost(self, reason=None):
        if self.looping_calls is None: return
        for call in self.looping_calls.itervalues():
            call.stop()

    def logReport(self, filepath):
        with filepath.open("r") as handle:
            handle.seek(self.logs_seek[filepath])

            for line in handle:
                delim = self.logs[filepath][2]
                game = parse_xlogfile_line(line, delim)
                game["variant"] = self.logs[filepath][1]
                game["dumpfmt"] = self.logs[filepath][3]
                for line in self.logs[filepath][0](game):
                    self.say(CHANNEL, line)

            self.logs_seek[filepath] = handle.tell()

if __name__ == "__builtin__":
    f = protocol.ReconnectingClientFactory()
    f.protocol = DeathBotProtocol
    application = service.Application("DeathBot")
    deathservice = internet.SSLClient(HOST, PORT, f,
                                      ssl.ClientContextFactory())
    deathservice.setServiceParent(application)
