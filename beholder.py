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
import datetime
import ast

# znc
HOST, PORT = "chat.us.freenode.net", 6697
CHANNEL = "#hardfought"

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
xlogfile_parse["starttime"] = fromtimestamp_int
xlogfile_parse["curtime"] = fromtimestamp_int
xlogfile_parse["endtime"] = fromtimestamp_int
xlogfile_parse["realtime"] = timedelta_int
#xlogfile_parse["deathdate"] = xlogfile_parse["birthdate"] = isodate

def parse_xlogfile_line(line, delim):
    record = {}
    for field in line.strip().split(delim):
        key, _, value = field.partition("=")
        if key in xlogfile_parse:
            value = xlogfile_parse[key](value)
        record[key] = value
    return record

def xlogfile_entries(fp):
    if fp is None: return
    with fp.open("rt") as handle:
        for line in handle:
            yield parse_xlogfile_line(line)

class DeathBotProtocol(irc.IRCClient):
    nickname = "Beholder"
    username = "beholder"
    realname = "Beholder"
    try:
        password = open("/opt/beholder/pw", "r").read().strip()
    except:
        pass

    sourceURL = "https://ascension.run/deathbot.py"
    versionName = "deathbot.py"
    versionNum = "0.1"

    xlogfiles = {filepath.FilePath("/opt/nethack/hardfought.org/nh343/var/xlogfile"): ("nh", ":"),
                 filepath.FilePath("/opt/nethack/hardfought.org/nhdev/var/xlogfile"): ("nd", "\t"),
                 filepath.FilePath("/opt/nethack/hardfought.org/gh020/var/xlogfile"): ("gh", ":"),
                 filepath.FilePath("/opt/nethack/hardfought.org/un531/var/unnethack/xlogfile"): ("un", ":")}
    livelogs  = {filepath.FilePath("/opt/nethack/hardfought.org/nh343/var/livelog"): ("nh", ":"),
                 filepath.FilePath("/opt/nethack/hardfought.org/nhdev/var/livelog"): ("nd", "\t"),
                 filepath.FilePath("/opt/nethack/hardfought.org/gh020/var/livelog"): ("gh", ":"),
                 filepath.FilePath("/opt/nethack/hardfought.org/un531/var/unnethack/livelog"): ("un", ":")}

    looping_calls = None

    def signedOn(self):
        self.factory.resetDelay()
        self.startHeartbeat()
        self.join(CHANNEL)

        self.logs = {}
        for xlogfile, (variant, delim) in self.xlogfiles.iteritems():
            self.logs[xlogfile] = (self.xlogfileReport, variant, delim)
        for livelog, (variant, delim) in self.livelogs.iteritems():
            self.logs[livelog] = (self.livelogReport, variant, delim)

        self.logs_seek = {}
        self.looping_calls = {}

        for filepath in self.logs:
            with filepath.open("r") as handle:
                handle.seek(0, 2)
                self.logs_seek[filepath] = handle.tell()

            self.looping_calls[filepath] = task.LoopingCall(self.logReport, filepath)
            self.looping_calls[filepath].start(3)

    def startscummed(self, game):
        return game["death"] in ("quit", "escaped") and game["points"] < 1000

    def xlogfileReport(self, game):
        if self.startscummed(game): return

        if game.get("charname", False):
            if game.get("name", False):
                if game["name"] != game["charname"]:
                    game["name"] = "{charname} ({name})".format(**game)
            else:
                game["name"] = game["charname"]

        if (game.get("mode", "normal") == "normal" and
              game.get("modes", "normal") == "normal"):
            yield ("[{variant}] {name} ({role} {race} {gender} {align}), "
                   "{points} points, T:{turns}, {death}").format(**game)
        else:
            if "modes" in game:
                if game["modes"].startswith("normal,"):
                    game["mode"] = game["modes"][7:]
                else:
                    game["mode"] = game["modes"]
            yield ("[{variant}] {name} ({role} {race} {gender} {align}), "
                   "{points} points, T:{turns}, {death}, "
                   "in {mode} mode").format(**game)

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
