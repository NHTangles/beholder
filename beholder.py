"""
beholder.py - a game-reporting and general services IRC bot for
              the hardfought.org NetHack server.

tnnt branch (2018) - adaptation specifically for the tnnt tournament

Copyright (c) 2018 A. Thomson, K. Simpson
Based on original code from:
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
import datetime # for timestamp stuff
import time     # for !time
import ast      # for conduct/achievement bitfields - not really used
import os       # for check path exists (dumplogs), and chmod
import stat     # for chmod mode bits
import re       # for hello, and other things.
import urllib   # for dealing with NH4 variants' #&$#@ spaces in filenames.
import shelve   # for persistent !tell messages
import random   # for !rng and friends
import glob     # for matching in !whereis

from botconf import HOST, PORT, CHANNEL, NICK, USERNAME, REALNAME, BOTDIR
from botconf import PWFILE, FILEROOT, WEBROOT, LOGROOT, PINOBOT, ADMIN
from botconf import SERVERTAG
try: from botconf import DCBRIDGE
except: DCBRIDGE = None
try: from botconf import TEST
except: TEST = False
try:
    from botconf import REMOTES
except:
    SLAVE = True #if we have no slaves, we (probably) are the slave
    REMOTES = {}
try:
    from botconf import MASTERS
except:
    SLAVE = False #if we have no master we (definitely) are the master
    MASTERS = []

def fromtimestamp_int(s):
    return datetime.datetime.fromtimestamp(int(s))

def timedelta_int(s):
    return datetime.timedelta(seconds=int(s))

def isodate(s):
    return datetime.datetime.strptime(s, "%Y%m%d").date()

def fixdump(s):
    return s.replace("_",":")

xlogfile_parse = dict.fromkeys(
    ("points", "deathdnum", "deathlev", "maxlvl", "hp", "maxhp", "deaths",
     "uid", "turns", "xplevel", "exp","depth","dnum","score","amulet"), int)
xlogfile_parse.update(dict.fromkeys(
    ("conduct", "event", "carried", "flags", "achieve"), ast.literal_eval))
#xlogfile_parse["starttime"] = fromtimestamp_int
#xlogfile_parse["curtime"] = fromtimestamp_int
#xlogfile_parse["endtime"] = fromtimestamp_int
#xlogfile_parse["realtime"] = timedelta_int
#xlogfile_parse["deathdate"] = xlogfile_parse["birthdate"] = isodate
#xlogfile_parse["dumplog"] = fixdump

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
    username = USERNAME
    realname = REALNAME
    admin = ADMIN
    slaves = {}
    for r in REMOTES:
        slaves[REMOTES[r][1]] = r
    # if we're the master, include ourself on the slaves list
    if not SLAVE:
        slaves[NICK] = [WEBROOT,NICK,FILEROOT]
        #...and the masters list
        MASTERS += [NICK]
    try:
        password = open(PWFILE, "r").read().strip()
    except:
        password = "NotTHEPassword"

    sourceURL = "https://github.com/NHTangles/beholder"
    versionName = "beholder.py (tnnt)"
    versionNum = "0.1"

    dump_url_prefix = WEBROOT + "userdata/{name[0]}/{name}/"
    dump_file_prefix = FILEROOT + "dgldir/userdata/{name[0]}/{name}/"

    if not SLAVE:
        scoresURL = "https://www.hardfought.org/tnnt/trophies.html or https://www.hardfought.org/tnnt/clans.html"
        rceditURL = WEBROOT + "nethack/rcedit"
        helpURL = WEBROOT + "nethack"
        logday = time.strftime("%d")
        chanLogName = LOGROOT + CHANNEL + time.strftime("-%Y-%m-%d.log")
        chanLog = open(chanLogName,'a')
        os.chmod(chanLogName,stat.S_IRUSR|stat.S_IWUSR|stat.S_IRGRP|stat.S_IROTH)

    xlogfiles = {filepath.FilePath(FILEROOT+"tnnt/var/xlogfile"): ("tnnt", "\t", "tnnt/dumplog/{starttime}.tnnt.txt")}
    livelogs  = {filepath.FilePath(FILEROOT+"tnnt/var/livelog"): ("tnnt", "\t")}

    # for displaying variants and server tags in colour
    displaystring = {"hdf-us" : "\x1D\x0304US\x03\x0F",
                     "hdf-au" : "\x1D\x0303AU\x03\x0F",
                     "hdf-eu" : "\x1D\x0312EU\x03\x0F"}

    # put the displaystring for a thing in square brackets
    def displaytag(self, thing):
       return '[' + self.displaystring.get(thing,thing) + ']'

    # for !who or !players or whatever we end up calling it
    # Reduce the repetitive crap
    DGLD=FILEROOT+"dgldir/"
    INPR=DGLD+"inprogress-"
    inprog = {"tnnt" : [INPR+"tnnt/"]}

    # for !whereis
    whereis = {"tnnt": [FILEROOT+"tnnt/var/whereis/"]}

    dungeons = ["The Dungeons of Doom","Gehennom","The Gnomish Mines","The Quest",
                          "Sokoban","Fort Ludios","Vlad's Tower","The Elemental Planes"]

    looping_calls = None

    # SASL auth nonsense required if we run on AWS
    # copied from https://github.com/habnabit/txsocksx/blob/master/examples/tor-irc.py
    # irc_CAP and irc_9xx are UNDOCUMENTED.
    def connectionMade(self):
        self.sendLine('CAP REQ :sasl')
        #self.deferred = Deferred()
        irc.IRCClient.connectionMade(self)

    def irc_CAP(self, prefix, params):
        if params[1] != 'ACK' or params[2].split() != ['sasl']:
            print 'sasl not available'
            self.quit('')
        sasl = ('{0}\0{0}\0{1}'.format(self.nickname, self.password)).encode('base64').strip()
        self.sendLine('AUTHENTICATE PLAIN')
        self.sendLine('AUTHENTICATE ' + sasl)

    def irc_903(self, prefix, params):
        self.sendLine('CAP END')

    def irc_904(self, prefix, params):
        print 'sasl auth failed', params
        self.quit('')
    irc_905 = irc_904

    def signedOn(self):
        self.factory.resetDelay()
        self.startHeartbeat()
        if not SLAVE: self.join(CHANNEL)
        random.seed()

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

        # streaks
        self.curstreak = {}
        self.longstreak = {}

        # ascensions (for !asc)
        # "!asc plr" will give asc stats for player.
        # "!asc" will be as above, assuming requestor's nick.
        # asc[player][role] = count;
        # asc[player][race] = count;
        # asc[player][align] = count;
        # asc[player][gender] = count;
        # assumes 3-char abbreviations for role/race/align/gender, and no overlaps.
        # for asc ratio we need total games too
        # allgames[player] = count;
        self.asc = {}
        self.allgames = {}

        # for !tell
        self.tellbuf = shelve.open(BOTDIR + "/tellmsg.db", writeback=True)
        # for !setmintc
        self.plr_tc = shelve.open(BOTDIR + "/plrtc.db", writeback=True)

        # Commands must be lowercase here.
        self.commands = {"ping"     : self.doPing,
                         "time"     : self.doTime,
                         "tell"     : self.takeMessage,
                         "source"   : self.doSource,
                         "lastgame" : self.multiServerCmd,
                         "lastasc"  : self.multiServerCmd,
                         "scores"   : self.doScoreboard,
                         "sb"       : self.doScoreboard,
                         "rcedit"   : self.doRCedit,
                         "commands" : self.doCommands,
                         "help"     : self.doHelp,
                         "players"  : self.multiServerCmd,
                         "who"      : self.multiServerCmd,
                         "asc"      : self.multiServerCmd,
                         "streak"   : self.multiServerCmd,
                         "whereis"  : self.multiServerCmd,
                         # these ones are for control messages between master and slaves
                         # sender is checked, so these can't be used by the public
                         "#q#"      : self.doQuery,
                         "#r#"      : self.doResponse}
        # commands executed based on contents of #Q# message
        self.qCommands = {"players" : self.getPlayers,
                          "who"     : self.getPlayers,
                          "whereis" : self.getWhereIs,
                          "asc"     : self.getAsc,
                          "streak"  : self.getStreak,
                          "lastasc" : self.getLastAsc,
                          "lastgame": self.getLastGame}
        # callbacks to run when all slaves have responded
        self.callBacks = {"players" : self.outPlayers,
                          "who"     : self.outPlayers,
                          "whereis" : self.outWhereIs,
                          "asc"     : self.outAscStreak,
                          "streak"  : self.outAscStreak,
                          # TODO: timestamp these so we can report the very last one
                          # For now, use the !asc/!streak callback as it's generic enough
                          "lastasc" : self.outAscStreak,
                          "lastgame": self.outAscStreak}

        # checkUsage outputs a message and returns false if input is bad
        # returns true if input is ok
        self.checkUsage ={"whereis" : self.usageWhereIs,
                          "asc"     : self.usageAsc,
                          "streak"  : self.usageStreak}

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

        # poll logs for updates every 3 seconds
        for filepath in self.logs:
            self.looping_calls[filepath] = task.LoopingCall(self.logReport, filepath)
            self.looping_calls[filepath].start(3)

        # Additionally, keep an eye on our nick to make sure it's right.
        # Perhaps we only need to set this up if the nick was originally
        # in use when we signed on, but a 30-second looping call won't kill us
        self.looping_calls["nick"] = task.LoopingCall(self.nickCheck)
        self.looping_calls["nick"].start(30)

    def nickCheck(self):
        # also rejoin the channel here, in case we drop off for any reason
        if not SLAVE: self.join(CHANNEL)
        if (self.nickname != NICK):
            self.setNick(NICK)

    def nickChanged(self, nn):
        # catch successful changing of nick from above and identify with nickserv
        self.msg("NickServ", "identify " + nn + " " + self.password)

    def logRotate(self):
        self.chanLog.close()
        self.logday = time.strftime("%d")
        self.chanLogName = LOGROOT + CHANNEL + time.strftime("-%Y-%m-%d.log")
        self.chanLog = open(self.chanLogName,'a') # 'w' is probably fine here
        os.chmod(self.chanLogName,stat.S_IRUSR|stat.S_IWUSR|stat.S_IRGRP|stat.S_IROTH)

    def stripText(self, msg):
        # strip the colour control stuff out
        # This can probably all be done with a single RE but I have a headache.
        message = re.sub(r'\x03\d\d,\d\d', '', msg) # fg,bg pair
        message = re.sub(r'\x03\d\d', '', message) # fg only
        message = re.sub(r'[\x1D\x03\x0f]', '', message) # end of colour and italics
        return message

    # Write log
    def log(self, message):
        if SLAVE: return
        message = self.stripText(message)
        if time.strftime("%d") != self.logday: self.logRotate()
        self.chanLog.write(time.strftime("%H:%M ") + message + "\n")
        self.chanLog.flush()

    # wrapper for "msg" that logs if msg dest is channel
    # Need to log our own actions separately as they don't trigger events
    def msgLog(self, replyto, message):
        if replyto == CHANNEL:
            self.log("<" + self.nickname + "> " + message)
        self.msg(replyto, message)

    # Similar wrapper for describe
    def describeLog(self,replyto, message):
        if replyto == CHANNEL:
            self.log("* " + self.nickname + " " + message)
        self.describe(replyto, message)

    # construct and send response.
    # replyto is channel, or private nick
    # sender is original sender of query
    def respond(self, replyto, sender, message):
        if (replyto.lower() == sender.lower()): #private
            self.msg(replyto, message)
        else: #channel - prepend "Nick: " to message
            self.msgLog(replyto, sender + ": " + message)

    # Query/Response handling
    def doQuery(self, sender, replyto, msgwords):
        # called when slave gets queried by master.
        # msgwords is [ #Q#, <query_id>, <orig_sender>, <command>, ... ]
        if (sender in MASTERS) and (msgwords[3] in self.qCommands):
            # sender is passed to master; msgwords[2] is passed tp sender
            self.qCommands[msgwords[3]](sender,msgwords[2],msgwords[1],msgwords[3:])
        else:
            print "Bogus slave query from " + sender + ": " + " ".join(msgwords);

    def doResponse(self, sender, replyto, msgwords):
        # called when slave returns query response to master
        # msgwords is [ #R#, <query_id>, [server-tag], command output, ...]
        if sender in self.slaves and msgwords[1] in self.queries:
            self.queries[msgwords[1]]["resp"][sender] = " ".join(msgwords[2:])
            if set(self.queries[msgwords[1]]["resp"].keys()) >= set(self.slaves.keys()):
                #all slaves have responded
                self.queries[msgwords[1]]["callback"](self.queries.pop(msgwords[1]))
        else:
            print "Bogus slave response from " + sender + ": " + " ".join(msgwords);


    # implement commands here
    def doPing(self, sender, replyto, msgwords):
        self.respond(replyto, sender, "Pong! " + " ".join(msgwords[1:]))

    def doTime(self, sender, replyto, msgwords):
        self.respond(replyto, sender, time.strftime("The time is %H:%M:%S(%Z) on %A, %B %d, %Y"))
        timeLeft = self.countDown()
        if timeLeft["countdown"] <= timedelta(0):
            self.msgLog(c, "The " + YEAR + " tournament is OVER!")
            return
        verbs = { "start" : "begins",
                  "end" : "closes"
                }

        self.respond(replyto, sender, "The time remaining until the " + YEAR + " Tournament "
                                      + verbs[timeLeft["event"]]
                                      + " is '00-00-{days:0>2}:{hours:0>2}-{minutes:0>2}-{seconds:0>2}'".format(**timeLeft))


    def doSource(self, sender, replyto, msgwords):
        self.respond(replyto, sender, self.sourceURL )

    def doScoreboard(self, sender, replyto, msgwords):
        self.respond(replyto, sender, self.scoresURL )

    def doRCedit(self, sender, replyto, msgwords):
        self.respond(replyto, sender, self.rceditURL )

    def doHelp(self, sender, replyto, msgwords):
        self.respond(replyto, sender, self.helpURL )

    def doCommands(self, sender, replyto, msgwords):
        self.respond(replyto, sender, "available commands are !help !ping !time !tell !source !lastgame !lastasc !asc !streak !rcedit !scores !sb !whereis !players !who !commands")

    def takeMessage(self, sender, replyto, msgwords):
        rcpt = msgwords[1].split(":")[0] # remove any trailing colon - could check for other things here.
        message = " ".join(msgwords[2:])
        if (replyto == sender): #this was a privmsg
            forwardto = rcpt # so we pass a privmsg
            # and mark it so rcpt knows it was sent privately
            message = "[private] " + message
        else: # !tell on channel
            forwardto = replyto # so pass to channel
        if not self.tellbuf.get(rcpt.lower(),False):
            self.tellbuf[rcpt.lower()] = []
        self.tellbuf[rcpt.lower()].append((forwardto,sender,time.time(),message))
        self.tellbuf.sync()
        self.msgLog(replyto,"Will do, " + sender + "!")

    def msgTime(self, stamp):
        # Timezone handling is not great, but the following seems to work.
        # assuming TZ has not changed between leaving & taking the message.
        return datetime.datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M") + time.strftime(" %Z")

    def checkMessages(self, user):
        # this runs every time someone speaks on the channel,
        # so return quickly if there's nothing to do
        # but first... deal with the "bonus" colours and leading @ symbols of discord users
        if user[0] == '@':
            plainuser = self.stripText(user).lower()
            if not self.tellbuf.get(plainuser,None):
                plainuser = plainuser[1:] # strip the leading @ and try again (below)
        else:
            plainuser = user.lower()
        if not self.tellbuf.get(plainuser,None): return
        nicksfrom = []
        if len(self.tellbuf[plainuser]) > 2 and user[0] != '@':
            for (forwardto,sender,ts,message) in self.tellbuf[plainuser]:
                if forwardto.lower() != user.lower(): # don't add sender to list if message was private
                    if sender not in nicksfrom: nicksfrom += [sender]
                self.respond(user,user, "Message from " + sender + " at " + self.msgTime(ts) + ": " + message)
            # "tom" "tom and dick" "tom, dick, and harry"
            fromstr = ""
            for (i,n) in enumerate(nicksfrom):
                # first item
                if (i == 0):
                    fromstr = n
                # last item
                elif (i == len(nicksfrom)-1):
                    if (i > 1): fromstr += "," # oxford comma :P
                    fromstr += " and " + n
                # middle items
                else:
                   fromstr += ", " + n

            if fromstr: # don't say anything if all messages were private
                self.respond(CHANNEL, user, "Messages from " + fromstr + " have been forwarded to you privately.");

        else:
            for (forwardto,sender,ts,message) in self.tellbuf[plainuser]:
                self.respond(forwardto, user, "Message from " + sender + " at " + self.msgTime(ts) + ": " + message)
        del self.tellbuf[plainuser]
        self.tellbuf.sync()

    QUERY_ID = 0 # just use a sequence number for now
    def newQueryId(self):
        self.QUERY_ID += 1
        return str(self.QUERY_ID)

    queries = {}

    def forwardQuery(self,sender,replyto,msgwords,callback):
        # [Here]
        # Store a query reference locally, indexed by a unique identifier
        # Store a callback function for when everyone responds to the query.
        # forward the query tagged with the ID to the slaves.
        # [elsewhere]
        # record query responses, and call callback when all received (or timeout)
        # This all becomes easier if we just treat ourself (master) as one of the slaves
        q = self.newQueryId()
        self.queries[q] = {}
        self.queries[q]["callback"] = callback
        self.queries[q]["replyto"] = replyto
        self.queries[q]["sender"] = sender
        self.queries[q]["resp"] = {}
        message = "#Q# " + " ".join([q,sender] + msgwords)

        for sl in self.slaves.keys():
            print "forwardQuery: " + sl
            self.msg(sl,message)

    # Multi-server command entry point (forwards query to slaves)
    def multiServerCmd(self, sender, replyto, msgwords):
        if msgwords[0] in self.checkUsage:
            if not self.checkUsage[msgwords[0]](sender, replyto, msgwords):
                return
        if self.slaves:
            self.forwardQuery(sender, replyto, msgwords, self.callBacks.get(msgwords[0],None))

    # !players - respond to forwarded query and actually pull the info
    def getPlayers(self, master, sender, query, msgwords):
        plrvar = ""
        for var in self.inprog.keys():
            for inpdir in self.inprog[var]:
                for inpfile in glob.iglob(inpdir + "*.ttyrec"):
                    # /stuff/crap/PLAYER:shit:garbage.ttyrec
                    # we want AFTER last '/', BEFORE 1st ':'
                    plrvar += inpfile.split("/")[-1].split(":")[0] + " "
        if len(plrvar) == 0:
            plrvar = "No current players"
        response = "#R# " + query + " " + self.displaytag(SERVERTAG) + " " + plrvar
        self.msg(master, response)

    # !players callback. Actually print the output.
    def outPlayers(self,q):
        outmsg = " | ".join(q["resp"].values())
        self.respond(q["replyto"],q["sender"],outmsg)

    def usageWhereIs(self, sender, replyto, msgwords):
        if (len(msgwords) != 2):
            self.respond(replyto, sender, "!" + msgwords[0] + " <player> - finds a player in the dungeon." + replytag)
            return False
        return True

    def getWhereIs(self, master, sender, query, msgwords):
        ammy = ["", " (with Amulet)"]
        # look for inrpogress file first, only report active games
        for var in self.inprog.keys():
            for inpdir in self.inprog[var]:
                for inpfile in glob.iglob(inpdir + "*.ttyrec"):
                    plr = inpfile.split("/")[-1].split(":")[0]
                    if plr.lower() == msgwords[1].lower():
                        for widir in self.whereis[var]:
                            for wipath in glob.iglob(widir + "*.whereis"):
                                if wipath.split("/")[-1].lower() == (msgwords[1] + ".whereis").lower():
                                    plr = wipath.split("/")[-1].split(".")[0] # Correct case
                                    wirec = parse_xlogfile_line(open(wipath, "r").read().strip(),":")

                                    self.msg(master, "#R# " + query
                                             + " " + self.displaytag(SERVERTAG) + " " + plr
                                             + " : ({role} {race} {gender} {align}) T:{turns} ".format(**wirec)
                                             + self.dungeons[wirec["dnum"]]
                                             + " level: " + str(wirec["depth"])
                                             + ammy[wirec["amulet"]])
                                    return

                        self.msg(master, "#R# " + query + " "
                                                + self.displaytag(SERVERTAG)
                                                + " " + plr + " "
                                                + ": No details available")
                        return
        self.msg(master, "#R# " + query + " " + self.displaytag(SERVERTAG)
                                        + " " + msgwords[1]
                                        + " is not currently playing on this server.")

    def outWhereIs(self,q):
        player = ''
        msgs = []
        for server in q["resp"]:
            if " is not currently playing" in q["resp"][server]:
                player = q["resp"][server].split(" ")[1]
            else:
                msgs += [q["resp"][server]]
        outmsg = " | ".join(msgs)
        if not outmsg: outmsg = player + " is not playing."
        self.respond(q["replyto"],q["sender"],outmsg)


    def usageAsc(self, sender, replyto, msgwords):
        if len(msgwords) < 3:
            return True
        return False

    def getAsc(self, master, sender, query, msgwords):
        if len(msgwords) == 2:
            PLR = msgwords[1]
        else:
            PLR = sender
        if not PLR: return # bogus input, should have been handled in usage check above
        plr = PLR.lower()
        stats = ""
        totasc = 0
        if not plr in self.asc:
            repl = self.displaytag(SERVERTAG) + " No ascensions for " + PLR
            if plr in self.allgames:
                repl += " in " + str(self.allgames[plr]) + " games"
            repl += "."
            self.msg(master,"#R# " + query + " " + repl)
            return
        for role in config["nethack"]["roles"]:
             role = role.title() # capitalise the first letter
             if role in self.asc[plr]:
                totasc += self.asc[plr][role]
                stats += " " + str(self.asc[plr][role]) + "x" + role
        stats += ", "
        for race in config["nethack"]["races"]:
            race = race.title()
            if race in self.asc[plr]:
                stats += " " + str(self.asc[plr][race]) + "x" + race
        stats += ", "
        for alig in config["nethack"]["aligns"]:
            if alig in self.asc[plr]:
                stats += " " + str(self.asc[plr][alig]) + "x" + alig
        stats += ", "
        for gend in config["nethack"]["genders"]:
            if gend in self.asc[plr]:
                stats += " " + str(self.asc[plr][gend]) + "x" + gend
        stats += "."
        self.msg(master, "#R# " + query + " " + self.displaytag(SERVERTAG)
                         + " " + PLR
                         + " has ascended " 
                         + str(totasc) + " times in "
                         + str(self.allgames[plr])
                         + " games ({:0.2f}%):".format((100.0 * totasc)
                                               / self.allgames[plr])
                         + stats)
        return

    def outAscStreak(self,q):
        msgs = []
        for server in q["resp"]:
            if q["resp"][server].split(' ')[0] == 'No':
                # If they all say "No streaks for bob", that becomes the eventual output
                fallback_msg = q["resp"][server]
            else:
               msgs += [q["resp"][server]]
        outmsg = " | ".join(msgs)
        if not outmsg: outmsg = fallback_msg
        self.respond(q["replyto"],q["sender"],outmsg)

    def usageStreak(self, sender, replyto, msgwords):
        if len(msgwords) > 2: return False
        return True

    def streakDate(self,stamp):
        return datetime.datetime.fromtimestamp(float(stamp)).strftime("%Y-%m-%d")

    def getStreak(self, master, sender, query, msgwords):
        if len(msgwords) == 2:
            PLR = msgwords[1]
        else:
            PLR = sender
        if not PLR: return # bogus input, handled by usage check.
        plr = PLR.lower()
        reply = "#R# " + query + " "
        (lstart,lend,llength) = self.longstreak.get(plr,(0,0,0))
        (cstart,cend,clength) = self.curstreak.get(plr,(0,0,0))
        if llength == 0:
            reply += "No streaks for " + PLR + "."
            self.msg(master,reply)
            return
        reply += self.displaytag(SERVERTAG) + " " + PLR 
        reply += " Max: " + str(llength) + " (" + self.streakDate(lstart) \
                          + " - " + self.streakDate(lend) + ")"
        if clength > 0:
            if cstart == lstart:
                reply += "(current)"
            else:
                reply += ". Current: " + str(clength) + " (since " \
                                       + self.streakDate(cstart) + ")"
        reply += "."
        self.msg(master,reply)
        return

    def getLastGame(self, master, sender, query, msgwords):
        if (len(msgwords) >= 2): #player specified
            plr = msgwords[1].lower()
            dl = self.lg.get(plr,False)
            if not dl:
                self.msg(master, "#R# " + query +
                                 " No last game for " + msgwords[1] + ".")
                return
            self.msg(master, "#R# " + query + " " + self.displaytag(SERVERTAG) + " " + dl)
            return
        # no player
        self.msg(master, "#R# " + query + " " + self.displaytag(SERVERTAG) + " " + self.lastgame)

    def getLastAsc(self, master, sender, query, msgwords):
        if (len(msgwords) >= 2):  #player specified
            plr = msgwords[1].lower()
            dl = self.la.get(plr,False)
            if not dl:
                self.msg(master, "#R# " + query +
                                 " No last ascension for " + msgwords[1] + ".")
                return
            self.msg(master, "#R# " + query + " " + self.displaytag(SERVERTAG) + " " + dl)
            return
        self.msg(master, "#R# " + query + " " + self.displaytag(SERVERTAG) + " " + self.lastasc)

    # Listen to the chatter
    def privmsg(self, sender, dest, message):
        sender = sender.partition("!")[0]
        if SLAVE and sender not in MASTERS: return
        if (sender == PINOBOT): # response to earlier pino query
            self.msgLog(CHANNEL,message)
            return
        if (dest == CHANNEL): #public message
            self.log("<"+sender+"> " + message)
            replyto = CHANNEL
            if (sender == DCBRIDGE and message[0] == '<'):
                msgparts = message[1:].split('> ')
                sender = msgparts[0]
                message = "> ".join(msgparts[1:]) # in case there's more "> " in the message
        else: #private msg
            replyto = sender
        # Hello processing first.
        if re.match(r'^(hello|hi|hey|salut|hallo|guten tag|shalom|ciao|hola|aloha|bonjour|hei|gday|konnichiwa|nuqneh)[!?. ]*$', message.lower()):
            self.doHello(sender, replyto)
        # Message checks next.
        self.checkMessages(sender)
        # Proxy pino queries
        if (message[0] == '@'):
            if (dest == CHANNEL):
                self.msg(PINOBOT,message)
            else:
                self.respond(replyto,sender,"Please query " + PINOBOT + " directly.")
            return
        # ignore other channel noise unless !command
        if (message[0] != '!'):
            if (dest == CHANNEL): return
        else: # pop the '!'
            message = message[1:]
        msgwords = message.strip().split(" ")
        if re.match(r'^\d*d\d*$', msgwords[0]):
            self.rollDice(sender, replyto, msgwords)
            return
        if self.commands.get(msgwords[0].lower(), False):
            self.commands[msgwords[0].lower()](sender, replyto, msgwords)
            return
        if dest != CHANNEL and sender in self.slaves: # game announcement from slave
            self.msg(CHANNEL, " ".join(msgwords))

    #other events for logging
    def action(self, doer, dest, message):
        if (dest == CHANNEL):
            doer = doer.split('!', 1)[0]
            self.log("* " + doer + " " + message)

    def userRenamed(self, oldName, newName):
        self.log("-!- " + oldName + " is now known as " + newName)

    def noticed(self, user, channel, message):
        if (channel == CHANNEL):
            user = user.split('!')[0]
            self.log("-" + user + ":" + channel + "- " + message)

    def modeChanged(self, user, channel, set, modes, args):
        if (set): s = "+"
        else: s = "-"
        user = user.split('!')[0]
        if args[0]:
            self.log("-!- mode/" + channel + " [" + s + modes + " " + " ".join(list(args)) + "] by " + user)
        else:
            self.log("-!- mode/" + channel + " [" + s + modes + "] by " + user)

    def userJoined(self, user, channel):
        #(user,details) = user.split('!')
        #self.log("-!- " + user + " [" + details + "] has joined " + channel)
        self.log("-!- " + user + " has joined " + channel)

    def userLeft(self, user, channel):
        #(user,details) = user.split('!')
        #self.log("-!- " + user + " [" + details + "] has left " + channel)
        self.log("-!- " + user + " has left " + channel)

    def userQuit(self, user, quitMsg):
        #(user,details) = user.split('!')
        #self.log("-!- " + user + " [" + details + "] has quit [" + quitMsg + "]")
        self.log("-!- " + user + " has quit [" + quitMsg + "]")

    def userKicked(self, kickee, channel, kicker, message):
        kicker = kicker.split('!')[0]
        kickee = kickee.split('!')[0]
        self.log("-!- " + kickee + " was kicked from " + channel + " by " + kicker + " [" + message + "]")

    def topicUpdated(self, user, channel, newTopic):
        user = user.split('!')[0]
        self.log("-!- " + user + " changed the topic on " + channel + " to: " + newTopic)


    ### Xlog/livelog event processing
    def startscummed(self, game):
        return game["death"] in ("quit", "escaped") and game["points"] < 1000

    def xlogfileReport(self, game, report = True):
        # lowercased name is used for lookups
        lname = game["name"].lower()
        # "allgames" for a player even counts scummed games
        if not lname in self.allgames:
            self.allgames[lname] = 0
        self.allgames[lname] += 1
        if self.startscummed(game): return

        dumplog = game.get("dumplog",False)
        # Need to figure out the dump path before messing with the name below
        dumpfile = (self.dump_file_prefix + game["dumpfmt"]).format(**game)
        dumpurl = "(sorry, no dump exists for {variant}:{name})".format(**game)
        if TEST or os.path.exists(dumpfile): # dump files may not exist on test system
            # quote only the game-specific part, not the prefix.
            # Otherwise it quotes the : in https://
            # assume the rest of the url prefix is safe.
            dumpurl = urllib.quote(game["dumpfmt"].format(**game))
            dumpurl = self.dump_url_prefix.format(**game) + dumpurl
        self.lg[lname] = dumpurl
        self.lastgame = dumpurl

        if game["death"][0:8] in ("ascended"):
            # append dump url to report for ascensions
            game["ascsuff"] = "\n" + dumpurl
            # !lastasc stats.
            self.la[lname] = dumpurl
            self.lastasc = dumpurl

            # !asc stats
            if not lname in self.asc: self.asc[lname] = {}
            if not game["role"]   in self.asc[lname]: self.asc[lname][game["role"]]   = 0
            if not game["race"]   in self.asc[lname]: self.asc[lname][game["race"]]   = 0
            if not game["gender"] in self.asc[lname]: self.asc[lname][game["gender"]] = 0
            if not game["align"]  in self.asc[lname]: self.asc[lname][game["align"]]  = 0
            self.asc[lname][game["role"]]   += 1
            self.asc[lname][game["race"]]   += 1
            self.asc[lname][game["gender"]] += 1
            self.asc[lname][game["align"]]  += 1

            # streaks
            (cs_start, cs_end, cs_length) = self.curstreak.get(lname,
                                                      (game["starttime"],0,0))
                cs_end = game["endtime"]
                cs_length += 1
                self.curstreak[lname] = (cs_start, cs_end, cs_length)
                (ls_start, ls_end,
                 ls_length) = self.longstreak.get(lname, (0,0,0))
                if cs_length > ls_length:
                    self.longstreak[lname] = self.curstreak[lname]

        else:   # not ascended - kill off any streak
            game["ascsuff"] = ""
            if lname in self.curstreak:
                del self.curstreak[lname]
        # end of statistics gathering

        if (not report): return # we're just reading through old entries at startup
        if self.plr_tc_notreached(game): return # ignore due to !setmintc

        # start of actual reporting
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
            yield ("{name} ({role} {race} {gender} {align}), "
                       "{points} points, T:{turns}, {death}{ascsuff}").format(**game)
        else:
            if "modes" in game:
                if game["modes"].startswith("normal,"):
                    game["mode"] = game["modes"][7:]
                else:
                    game["mode"] = game["modes"]
            yield ("{name} ({role} {race} {gender} {align}), "
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
            yield ("{player} ({role} {race} {gender} {align}) "
                   "{message}, on T:{turns}").format(**event)
        elif "wish" in event:
            yield ("{player} ({role} {race} {gender} {align}) "
                   'wished for "{wish}", on T:{turns}').format(**event)
        elif "shout" in event:
            yield ("{player} ({role} {race} {gender} {align}) "
                   'shouted "{shout}", on T:{turns}').format(**event)
        elif "bones_killed" in event:
            if not event.get("bones_rank",False): # fourk does not have bones rank so use role instead
                event["bones_rank"] = event["bones_role"]
            yield ("{player} ({role} {race} {gender} {align}) "
                   "killed the {bones_monst} of {bones_killed}, "
                   "the former {bones_rank}, on T:{turns}").format(**event)
        elif "killed_uniq" in event:
            yield ("{player} ({role} {race} {gender} {align}) "
                   "killed {killed_uniq}, on T:{turns}").format(**event)
        elif "defeated" in event: # fourk uses this instead of killed_uniq.
            yield ("{player} ({role} {race} {gender} {align}) "
                   "defeated {defeated}, on T:{turns}").format(**event)
        # more 1.3d shite
        elif "genocided_monster" in event:
            if event.get("dungeon_wide","yes") == "yes":
                event["genoscope"] = "dungeon wide";
            else:
                event["genoscope"] = "locally";
            yield ("{player} ({role} {race} {gender} {align}) "
                   "genocided {genocided_monster} {genoscope} on T:{turns}").format(**event)
        elif "shoplifted" in event:
            yield ("{player} ({role} {race} {gender} {align}) "
                   "stole {shoplifted} zorkmids of merchandise from the {shop} of"
                   " {shopkeeper} on T:{turns}").format(**event)
        elif "killed_shopkeeper" in event:
            yield ("{player} ({role} {race} {gender} {align}) "
                   "killed {killed_shopkeeper} on T:{turns}").format(**event)

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
                game["dumpfmt"] = self.logs[filepath][3]
                for line in self.logs[filepath][0](game):
                    line = self.displaytag(SERVERTAG) + " " + line
                    if SLAVE:
                        for master in MASTERS:
                            self.msg(master, line)
                    else:
                        self.msgLog(CHANNEL, line)

            self.logs_seek[filepath] = handle.tell()

if __name__ == "__builtin__":
    f = protocol.ReconnectingClientFactory()
    f.protocol = DeathBotProtocol
    application = service.Application("DeathBot")
    deathservice = internet.SSLClient(HOST, PORT, f,
                                      ssl.ClientContextFactory())
    deathservice.setServiceParent(application)
