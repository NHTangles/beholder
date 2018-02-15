"""
beholder.py - a game-reporting and general services IRC bot for
              the hardfought.org NetHack server.
Copyright (c) 2017 A. Thomson, K. Simpson
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
    try:
        password = open(PWFILE, "r").read().strip()
    except:
        password = "NotTHEPassword"

    sourceURL = "https://github.com/NHTangles/beholder"
    versionName = "beholder.py"
    versionNum = "0.1"

    dump_url_prefix = WEBROOT + "userdata/{name[0]}/{name}/"
    dump_file_prefix = FILEROOT + "dgldir/userdata/{name[0]}/{name}/"

    if not SLAVE:
        scoresURL = WEBROOT + "nethack/scoreboard (HDF) or https://scoreboard.xd.cm (ALL)"
        rceditURL = WEBROOT + "nethack/rcedit"
        helpURL = WEBROOT + "nethack"
        logday = time.strftime("%d")
        chanLogName = LOGROOT + CHANNEL + time.strftime("-%Y-%m-%d.log")
        chanLog = open(chanLogName,'a')
        os.chmod(chanLogName,stat.S_IRUSR|stat.S_IWUSR|stat.S_IRGRP|stat.S_IROTH)

    xlogfiles = {filepath.FilePath(FILEROOT+"nh343/var/xlogfile"): ("nh", ":", "nh343/dumplog/{starttime}.nh343.txt"),
                 filepath.FilePath(FILEROOT+"nhdev/var/xlogfile"): ("nd", "\t", "nhdev/dumplog/{starttime}.nhdev.txt"),
                 filepath.FilePath(FILEROOT+"grunthack-0.2.2/var/xlogfile"): ("gh", ":", "gh/dumplog/{starttime}.gh.txt"),
                 filepath.FilePath(FILEROOT+"dnethack-3.15.1/xlogfile"): ("dnh", ":", "dnethack/dumplog/{starttime}.dnh.txt"),
                 filepath.FilePath(FILEROOT+"fiqhackdir/data/xlogfile"): ("fh", ":", "fiqhack/dumplog/{dumplog}"),
                 filepath.FilePath(FILEROOT+"dynahack/dynahack-data/var/xlogfile"): ("dyn", ":", "dynahack/dumplog/{dumplog}"),
                 filepath.FilePath(FILEROOT+"nh4dir/save/xlogfile"): ("nh4", ":", "nethack4/dumplog/{dumplog}"),
                 filepath.FilePath(FILEROOT+"fourkdir/save/xlogfile"): ("4k", "\t", "nhfourk/dumps/{dumplog}"),
                 filepath.FilePath(FILEROOT+"sporkhack-0.6.5/var/xlogfile"): ("sp", "\t", "sporkhack/dumplog/{starttime}.sp.txt"),
                 filepath.FilePath(FILEROOT+"slex-2.1.7/xlogfile"): ("slex", "\t", "slex/dumplog/{starttime}.slex.txt"),
                 filepath.FilePath(FILEROOT+"un531/var/unnethack/xlogfile"): ("un", ":", "un531/dumplog/{starttime}.un531.txt.html")}
    livelogs  = {filepath.FilePath(FILEROOT+"nh343/var/livelog"): ("nh", ":"),
                 filepath.FilePath(FILEROOT+"nhdev/var/livelog"): ("nd", "\t"),
                 filepath.FilePath(FILEROOT+"grunthack-0.2.2/var/livelog"): ("gh", ":"),
                 filepath.FilePath(FILEROOT+"dnethack-3.15.1/livelog"): ("dnh", ":"),
                 filepath.FilePath(FILEROOT+"fourkdir/save/livelog"): ("4k", "\t"),
                 filepath.FilePath(FILEROOT+"fiqhackdir/data/livelog"): ("fh", ":"),
                 filepath.FilePath(FILEROOT+"sporkhack-0.6.5/var/livelog"): ("sp", ":"),
                 filepath.FilePath(FILEROOT+"slex-2.1.7/livelog"): ("slex", ":"),
                 filepath.FilePath(FILEROOT+"un531/var/unnethack/livelog"): ("un", ":")}

    # Forward events to other bots at the request of maintainers of other variant-specific channels
    forwards = {"nh" : [],
                "nd" : [],
              "zapm" : [],
                "gh" : [],
               "dnh" : [],
                "fh" : [],
               "dyn" : [],
               "nh4" : [],
                "4k" : [],
                "sp" : [],
              "slex" : ["FCCBot"],
                "un" : []}

    # for displaying variants and server tags in colour
    displaystring = {"nh" : "\x0315nh\x03",
                     "nd" : "\x0307nd\x03",
                   "zapm" : "\x0303zapm\x03",
                     "gh" : "\x0304gh\x03",
                    "dnh" : "\x0313dnh\x03",
                     "fh" : "\x0310fh\x03",
                    "dyn" : "\x0305dyn\x03",
                    "nh4" : "\x0306nh4\x03",
                     "4k" : "\x03114k\x03",
                     "sp" : "\x0314sp\x03",
                   "slex" : "\x0312slex\x03",
                     "un" : "\x0308un\x03",
                 "hdf-us" : "\x1D\x0304hdf-us\x03\x0F",
                 "hdf-eu" : "\x1D\x0312hdf-eu\x03\x0F"}

    # put the displaystring for a thing in square brackets
    def displaytag(self, thing):
       return '[' + self.displaystring.get(thing,thing) + ']'

    # for !who or !players or whatever we end up calling it
    # Reduce the repetitive crap
    DGLD=FILEROOT+"dgldir/"
    INPR=DGLD+"inprogress-"
    inprog = { "nh" : INPR+"nh343/",
               "nd" : INPR+"nhdev/",
             "zapm" : INPR+"zapm/",
               "gh" : INPR+"gh022/",
               "un" : INPR+"un531/",
              "dnh" : INPR+"dnh3151/",
               "fh" : INPR+"fh/",
               "4k" : INPR+"4k/",
              "nh4" : INPR+"nh4/",
               "sp" : INPR+"sp065/",
             "slex" : INPR+"slex217/",
              "dyn" : INPR+"dyn/"}

    # for !whereis
    # some of these don't exist yet, so paths may not be accurate
    whereis = {"nh": FILEROOT+"nh343/var/whereis/",
               "nd": FILEROOT+"nhdev/var/whereis/",
               "gh": FILEROOT+"grunthack-0.2.2/var/whereis/",
              "dnh": FILEROOT+"dnethack-3.15.1/whereis/",
               "fh": FILEROOT+"fiqhackdir/data/",
              "dyn": FILEROOT+"dynahack/dynahack-data/var/whereis/",
              "nh4": FILEROOT+"nh4dir/save/whereis/",
               "4k": FILEROOT+"fourkdir/save/",
               "sp": FILEROOT+"sporkhack-0.6.5/var/",
             "slex": FILEROOT+"slex-2.1.7/",
               "un": FILEROOT+"un531/var/unnethack/whereis/"}

    dungeons = {"nh": ["The Dungeons of Doom","Gehennom","The Gnomish Mines","The Quest",
                       "Sokoban","Fort Ludios","Vlad's Tower","The Elemental Planes"],
                "nd": ["The Dungeons of Doom","Gehennom","The Gnomish Mines","The Quest",
                       "Sokoban","Fort Ludios","Vlad's Tower","The Elemental Planes"],
                "gh": ["The Dungeons of Doom","Gehennom","The Gnomish Mines","The Quest",
                       "Sokoban","Fort Ludios","Vlad's Tower","The Elemental Planes"],
               "dnh": ["The Dungeons of Doom","Gehennom","The Gnomish Mines","Law Quest",
                       "Neutral Quest","The Lost Cities","Chaos Quest","The Quest",
                       "Sokoban","Fort Ludios","The Lost Tomb","The Sunless Sea",
                       "The Temple of Moloch","The Dispensary","Vlad's Tower",
                       "The Elemental Planes"],
                "fh": ["The Dungeons of Doom","Gehennom","The Gnomish Mines","The Quest",
                       "Sokoban","Fort Ludios","Vlad's Tower","The Elemental Planes"],
               "dyn": ["The Dungeons of Doom","Gehennom","The Gnomish Mines","The Quest",
                       "Sokoban","Town","Fort Ludios","One-eyed Sam's Market","Vlad's Tower",
                       "The Dragon Caves","The Elemental Planes","Advent Calendar"],
               "nh4": ["The Dungeons of Doom","Gehennom","The Gnomish Mines","The Quest",
                       "Sokoban","Fort Ludios","Vlad's Tower","The Elemental Planes"],
                "4k": ["The Dungeons of Doom","Gehennom","The Gnomish Mines","The Quest",
                       "Sokoban","Fort Ludios","Advent Calendar","Vlad's Tower",
                       "The Elemental Planes"],
                "sp": ["The Dungeons of Doom","Gehennom","The Gnomish Mines","The Quest",
                       "Sokoban","Fort Ludios","Vlad's Tower","The Elemental Planes"],
              "slex": ["The Dungeons of Doom","Gehennom","The Gnomish Mines","The Quest",
                       "Sokoban","Fort Ludios","Vlad's Tower","The Elemental Planes"],
                "un": ["The Dungeons of Doom","Gehennom","Sheol","The Gnomish Mines",
                       "The Quest","Sokoban","Town","The Ruins of Moria","Fort Ludios",
                       "One-eyed Sam's Market","Vlad's Tower","The Dragon Caves",
                       "The Elemental Planes","Advent Calendar"]}

    # variant related stuff that does not relate to xlogfile processing
    rolename = {"arc": "archeologist", "bar": "barbarian", "cav": "caveman",
                "hea": "healer",       "kni": "knight",    "mon": "monk",
                "pri": "priest",       "ran": "ranger",    "rog": "rogue",
                "sam": "samurai",      "tou": "tourist",   "val": "valkyrie",
                "wiz": "wizard", #all
                "ana": "anachrononaut",  "bin": "binder",    "nob": "Noble",
                "pir": "pirate",       "brd": "troubadour", #dnh
                "con": "convict"} #unnethack
    racename = {"dwa": "dwarf", "elf": "elf", "gno": "gnome", "hum": "human",
                "orc": "orc", #all
                "gia": "giant", "kob": "kobold", "ogr": "ogre", #grunt
                "clk": "clockwork automaton",    "bat": "chiropteran",
                "dro": "drow", "hlf": "half-dragon", "inc": "incantifier",
                "vam": "vampire", "swn": "yuki-onna", #dnh
                "scu": "scurrier", "syl": "sylph"} #fourk

    # save typing these out in multiple places
    vanilla_roles = ["arc","bar","cav","hea","kni","mon","pri",
                     "ran","rog","sam","tou","val","wiz"]
    vanilla_races = ["dwa","elf","gno","hum","orc"]

    # varname: ([aliases],[roles],[races])
    # first alias will be used for !variant
    # note this breaks if a player has the same name as an alias
    # so don't do that (I'm looking at you, FIQ)
    variants = {"nh": (["nh343", "nethack", "343"],
                       vanilla_roles, vanilla_races),
                "nd": (["nhdev", "nh361", "361dev", "361", "dev"],
                       vanilla_roles, vanilla_races),
                "nh4": (["nethack4", "n4"],
                       vanilla_roles, vanilla_races),
                "gh": (["grunt", "grunthack"],
                       vanilla_roles, vanilla_races + ["gia", "kob", "ogr"]),
               "dnh": (["dnethack", "dn"],
                       vanilla_roles
                         + ["ana", "bin", "nob", "pir", "brd", "con"],
                       vanilla_races
                         + ["clk", "bat", "dro", "hlf", "inc", "vam", "swn"]),
                "un": (["unnethack", "unh"],
                       vanilla_roles + ["con"], vanilla_races),
                "dyn": (["dynahack", "dyna"],
                       vanilla_roles + ["con"], vanilla_races + ["vam"]),
                "fh": (["fiqhack"], # not "fiq" see comment above
                       vanilla_roles, vanilla_races),
                "sp": (["sporkhack", "spork"],
                       vanilla_roles, vanilla_races),
                "4k": (["nhfourk", "nhf", "fourk"],
                       vanilla_roles, vanilla_races + ["gia", "scu", "syl"]),
              "slex": (["slex", "sloth"],
                       vanilla_roles
                         + ["aci", "act", "alt", "ama", "ana", "art", "ass",
                            "aug", "brd", "bin", "ble", "blo", "bos", "bul",
                            "cam", "cha", "che", "con", "coo", "cou", "abu",
                            "dea", "div", "dol", "mar", "sli", "drd", "dru",
                            "dun", "ele", "elm", "elp", "erd", "fai", "stu",
                            "fen", "fig", "fir", "fla", "fox", "gam", "gan",
                            "gee", "gla", "gof", "gol", "gra", "gun", "ice",
                            "scr", "jed", "jes", "jus", "kor", "kur", "lad",
                            "lib", "loc", "lun", "mah", "med", "mid", "mon",
                            "mur", "mus", "mys", "nec", "nin", "nob", "occ",
                            "off", "ord", "ota", "pal", "pic", "pir", "poi",
                            "pok", "pol", "pro", "psi", "rin", "roc", "sag",
                            "sai", "sci", "sha", "sla", "spa", "sup", "tha",
                            "top", "trs", "tra", "twe", "unb", "und", "unt",
                            "use", "wan", "war", "wil", "yeo", "sex", "zoo",
                            "zyb"],
                       vanilla_races
                         + ["add", "akt", "alb", "alc", "ali", "ame", "amn",
                            "anc", "acp", "agb", "ang", "aqu", "arg", "asg"])}

    # variants which support streaks - now tracking slex streaks, because that's totally possible.
    streakvars = ["nh", "nd", "gh", "dnh", "un", "sp", "slex"]
    # for !asc statistics - assume these are the same for all variants, or at least the sane ones.
    aligns = ["Law", "Neu", "Cha"]
    genders = ["Mal", "Fem"]

    #who is making tea? - bots of the nethack community who have influenced this project.
    brethren = ["Rodney", "Athame", "Arsinoe", "Izchak", "TheresaMayBot", "FCCBot", "the late Pinobot", "Announcy", "demogorgon", "the /dev/null/oracle", "NotTheOracle\\dnt"]
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
        # for populating lg/la per player at boot, we need to track game end times
        # variant and variant:player don't need this if we assume the xlogfiles are
        # ordered within variant.
        self.lge = {}
        self.tlastgame = 0
        self.lae = {}
        self.tlastasc = 0

        # streaks
        self.curstreak = {}
        self.longstreak = {}
        for v in self.streakvars:
            # curstreak[var][player] = (start, end, length)
            self.curstreak[v] = {}
            # longstreak - as above
            self.longstreak[v] = {}

        # ascensions (for !asc)
        # "!asc plr var" will give something like Rodney's output.
        # "!asc plr" will give breakdown by variant.
        # "!asc" or "!asc var" will be as above, assuming requestor's nick.
        # asc[var][player][role] = count;
        # asc[var][player][race] = count;
        # asc[var][player][align] = count;
        # asc[var][player][gender] = count;
        # assumes 3-char abbreviations for role/race/align/gender, and no overlaps.
        # for asc ratio we need total games too
        # allgames[var][player] = count;
        self.asc = {}
        self.allgames = {}
        for v in self.variants.keys():
            self.asc[v] = {};
            self.allgames[v] = {};

        # for !tell
        self.tellbuf = shelve.open(BOTDIR + "/tellmsg.db", writeback=True)
        # for !setmintc
        self.plr_tc = shelve.open(BOTDIR + "/plrtc.db", writeback=True)

        self.commands = {"ping"     : self.doPing,
                         "time"     : self.doTime,
                         "pom"      : self.doPom,
                         "porn"     : self.doPom,    #for Elronnd
                         "hello"    : self.doHello,
                         "beer"     : self.doBeer,
                         "tea"      : self.doTea,
                         "coffee"   : self.doTea,
                         "whiskey"  : self.doTea,
                         "whisky"   : self.doTea,
                         "vodka"    : self.doTea,
                         "rum"      : self.doTea,
                         "tequila"  : self.doTea,
                         "scotch"   : self.doTea,
                         "booze"    : self.doTea,
                         "potion"   : self.doTea,
                         "goat"     : self.doGoat,
                         "lotg"     : self.doLotg,
                         "rng"      : self.doRng,
                         "role"     : self.doRole,
                         "race"     : self.doRace,
                         "variant"  : self.doVariant,
                         "tell"     : self.takeMessage,
                         "source"   : self.doSource,
                         "lastgame" : self.lastGame,
                         "lastasc"  : self.lastAsc,
                         "scores"   : self.doScoreboard,
                         "sb"       : self.doScoreboard,
                         "rcedit"   : self.doRCedit,
                         "commands" : self.doCommands,
                         "help"     : self.doHelp,
                         "coltest"  : self.doColTest,
                         "players"  : self.doPlayers,
                         "who"      : self.doPlayers,
                         "asc"      : self.doAsc,
                         "streak"   : self.doStreak,
                         "whereis"  : self.doWhereIs,
                         "8ball"    : self.do8ball,
                         "setmintc" : self.setPlrTC}

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
                    if game["variant"] == "fh":
                        game["dumplog"] = fixdump(game["dumplog"])
                    if game["variant"] == "nh4":
                        game["dumplog"] = fixdump(game["dumplog"])
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

    #helper functions
    #lookup canonical variant id from alias
    def varalias(self,alias):
        alias = alias.lower()
        if alias in self.variants.keys(): return alias
        for v in self.variants.keys():
            if alias in self.variants[v][0]: return v
        # return original (lowercase) if not found.
        # this is used for variant/player agnosticism in !lastgame
        return alias

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

    # implement commands here
    def doPing(self, sender, replyto, msgwords):
        self.respond(replyto, sender, "Pong! " + " ".join(msgwords[1:]))

    def doTime(self, sender, replyto, msgwords):
        self.respond(replyto, sender, time.strftime("%c %Z"))

    def doSource(self, sender, replyto, msgwords):
        self.respond(replyto, sender, self.sourceURL )

    def doScoreboard(self, sender, replyto, msgwords):
        self.respond(replyto, sender, self.scoresURL )

    def doRCedit(self, sender, replyto, msgwords):
        self.respond(replyto, sender, self.rceditURL )

    def doHelp(self, sender, replyto, msgwords):
        self.respond(replyto, sender, self.helpURL )

    def doColTest(self, sender, replyto, msgwords):
        code = chr(3)
        code += msgwords[1]
        self.respond(replyto, sender, msgwords[1] + " " + code + "TEST!" )

    def doCommands(self, sender, replyto, msgwords):
        self.respond(replyto, sender, "available commands are !help !ping !time !pom !hello !booze !beer !potion !tea !coffee !whiskey !vodka !rum !tequila !scotch !goat !lotg !d(1-1000) !(1-50)d(1-1000) !8ball !rng !role !race !variant !tell !source !lastgame !lastasc !asc !streak !rcedit !scores !sb !setmintc !whereis !players !who !commands")

    def getPom(self, dt):
        # this is a direct translation of the NetHack method of working out pom.
        # I'm SURE there's easier ways to do this, but they may not give perfectly
        # consistent results with nh.
        # Note that timetuple gives diy 1..366, C/Perl libs give 0..365,
        # so need to adjust in final calculation.
        (year,m,d,H,M,S,diw,diy,ds) = dt.timetuple()
        goldn = (year % 19) + 1
        epact = (11 * goldn + 18) % 30
        if ((epact == 25 and goldn > 11) or epact == 24):
            epact += 1
        return ((((((diy-1 + epact) * 6) + 11) % 177) // 22) & 7)

    def doPom(self, sender, replyto, msgwords):
        # only info we have is that this yields 0..7, with 0 = new, 4 = full.
        # the rest is assumption.
        mp = ["new", "waxing crescent", "at first quarter", "waxing gibbous",
              "full", "waning gibbous", "at last quarter", "waning crescent"]
        dt = datetime.datetime.now()
        nowphase = self.getPom(dt)
        resp = "The moon is " + mp[nowphase]
        aday = datetime.timedelta(days=1)
        if nowphase in [0, 4]:
            daysleft = 1 # counting today
            dt += aday
            while self.getPom(dt) == nowphase:
                daysleft += 1
                dt += aday
            days = "days."
            if daysleft == 1: days = "day."
            resp += " for " + str(daysleft) + " more " + days
        else:
            daysuntil = 1 # again, we are counting today
            dt += aday
            while (self.getPom(dt)) not in [0, 4]:
               daysuntil += 1
               dt += aday
            days = " days."
            if daysuntil == 1: days = " day."
            resp += "; " + mp[self.getPom(dt)] + " moon in " + str(daysuntil) + days

        self.respond(replyto, sender, resp)

    def doHello(self, sender, replyto, msgwords = 0):
        self.msgLog(replyto, "Hello " + sender + ", Welcome to " + CHANNEL)

    def doLotg(self, sender, replyto, msgwords):
        if len(msgwords) > 1: target = " ".join(msgwords[1:])
        else: target = sender
        self.msgLog(replyto, "May the Luck of the Grasshopper be with you always, " + target + "!")

    def doGoat(self, sender, replyto, msgwords):
        act = random.choice(['kicks', 'rams', 'headbutts'])
        part = random.choice(['arse', 'nose', 'face', 'kneecap'])
        if len(msgwords) > 1:
            self.msgLog(replyto, sender + "'s goat runs up and " + act + " " + " ".join(msgwords[1:]) + " in the " + part + "! Baaaaaa!")
        else:
            self.msgLog(replyto, NICK + "'s goat runs up and " + act + " " + sender + " in the " + part + "! Baaaaaa!")

    def doRng(self, sender, replyto, msgwords):
        if len(msgwords) == 1:
            if (sender[0:11].lower()) == "grasshopper": # always troll the grasshopper
                self.msgLog(replyto, "The RNG only has eyes for you, " + sender)
            elif random.randrange(20): # 95% of the time, print usage
                self.respond(replyto, sender, "!rng thomas richard harold ; !rng do dishes|play nethack ; !rng 1-100")
            elif not random.randrange(5): #otherwise, trololol
                self.respond(replyto, sender, "How doth the RNG hate thee? Let me count the ways...")
            else:
                self.respond(replyto, sender, "The RNG " + random.choice(["hates you.",
                                                                          "is thinking of Grasshopper <3",
                                                                          "hates everyone (except you-know-who)",
                                                                          "cares not for your whining.",
                                                                          "is feeling generous (maybe).",
                                                                          "doesn't care.",
                                                                          "is indifferent to your plight."]))
            return
        multiword = [i.strip() for i in " ".join(msgwords[1:]).split('|')]
        if len(multiword) > 1:
            self.respond(replyto, sender, random.choice(multiword))
            return
        if len(msgwords) == 2:
            rngrange = msgwords[1].split('-')
            self.respond(replyto, sender, str(random.randrange(int(rngrange[0]), int(rngrange[-1])+1)))
        else:
            self.respond(replyto, sender, random.choice(msgwords[1:]))

    def rollDice(self, sender, replyto, msgwords):
        if re.match(r'^\d*d$', msgwords[0]): # !d, !4d is rubbish input.
            self.respond(replyto, sender, "No dice!")
            return
        dice = msgwords[0].split('d')
        if dice[0] == "": dice[0] = "1" #d6 -> 1d6
        (d0,d1) = (int(dice[0]),int(dice[1]))
        if d0 > 50:
            self.respond(replyto, sender, "Sorry, I don't have that many dice.")
            return
        if d1 > 1000:
            self.respond(replyto, sender, "Those dice are too big!")
            return
        (s, tot) = (None, 0)
        for i in range(0,d0):
            d = random.randrange(1,d1+1)
            if s: s += " + " + str(d)
            else: s = str(d)
            tot += d
        if "+" in s: s += " = " + str(tot)
        else: s = str(tot)
        self.respond(replyto, sender, s)

    def doRole(self, sender, replyto, msgwords):
        if len(msgwords) > 1:
           v = self.varalias(msgwords[1])
           #error if variant not found
           if not self.variants.get(v,False):
               self.respond(replyto, sender, "No variant " + msgwords[1] + " on server.")
               return
           self.respond(replyto, sender, self.rolename[random.choice(self.variants[v][1])])
        else:
           #pick variant first
           v = random.choice(self.variants.keys())
           self.respond(replyto, sender, self.variants[v][0][0] + " " + self.rolename[random.choice(self.variants[v][1])])

    def doRace(self, sender, replyto, msgwords):
        if len(msgwords) > 1:
           v = self.varalias(msgwords[1])
           #error if variant not found
           if not self.variants.get(v,False):
               self.respond(replyto, sender, "No variant " + msgwords[1] + " on server.")
           self.respond(replyto, sender, self.racename[random.choice(self.variants[v][2])])
        else:
           v = random.choice(self.variants.keys())
           self.respond(replyto, sender, self.variants[v][0][0] + " " + self.racename[random.choice(self.variants[v][2])])

    def doVariant(self, sender, replyto, msgwords):
        self.respond(replyto, sender, self.variants[random.choice(self.variants.keys())][0][0])

    def doBeer(self, sender, replyto, msgwords):
        self.respond(replyto, sender, random.choice(["It's your shout!", "I thought you'd never ask!",
                                                           "Burrrrp!", "We're not here to f#%k spiders, mate!",
                                                           "One Darwin stubby, coming up!"]))

    def do8ball(self, sender, replyto, msgwords):
        self.respond(replyto, sender, random.choice(["\x1DIt is certain\x0F", "\x1DIt is decidedly so\x0F", "\x1DWithout a doubt\x0F", "\x1DYes definitely\x0F", "\x1DYou may rely on it\x0F",
                                                           "\x1DAs I see it, yes\x0F", "\x1DMost likely\x0F", "\x1DOutlook good\x0F", "\x1DYes\x0F", "\x1DSigns point to yes\x0F", "\x1DReply hazy try again\x0F",
                                                           "\x1DAsk again later\x0F", "\x1DBetter not tell you now\x0F", "\x1DCannot predict now\x0F", "\x1DConcentrate and ask again\x0F",
                                                           "\x1DDon't count on it\x0F", "\x1DMy reply is no\x0F", "\x1DMy sources say no\x0F", "\x1DOutlook not so good\x0F", "\x1DVery doubtful\x0F"]))

    # The following started as !tea resulting in the bot making a cup of tea.
    # Now it does other stuff.
    bev = { "serves": ["delivers", "tosses", "passes", "pours", "hands", "throws"],
            # Attempt to make a sensible choice of vessel.
            # pick from "all", and check against specific drink. Loop a few times for a match, then give up.
            "vessel": {"all"   : ["cup", "mug", "shot", "tall glass", "tumbler", "glass", "schooner", "pint", "fifth", "vial", "potion", "barrel", "droplet", "bucket", "esky"],
                       "tea"   : ["cup", "mug"],
                       "potion": ["potion", "vial", "droplet"],
                       "booze" : ["shot", "tall glass", "tumbler", "glass", "schooner", "pint", "fifth", "barrel"],
                       "coffee": ["cup", "mug"],
                       "vodka" : ["shot", "tall glass", "tumbler", "glass"],
                       "whiskey":["shot", "tall glass", "tumbler", "glass"],
                       "rum"   : ["shot", "tall glass", "tumbler", "glass"],
                       "tequila":["shot", "tall glass", "tumbler", "glass"],
                       "scotch": ["shot", "tall glass", "tumbler", "glass"]
                       # others omitted - anything goes for them
                      },

            "drink" : {"tea"   : ["black", "white", "green", "polka-dot", "Earl Grey", "oolong", "darjeeling"],
                       "potion": ["water", "fruit juice", "see invisible", "sickness", "confusion", "extra healing", "hallucination", "healing", "holy water", "unholy water", "restore ability", "sleeping", "blindness", "gain energy", "invisibility", "monster detection", "object detection", "booze", "enlightenment", "full healing", "levitation", "polymorph", "speed", "acid", "oil", "gain ability", "gain level", "paralysis"],
                       "booze" : ["booze", "the hooch", "moonshine", "the sauce", "grog", "suds", "the hard stuff", "liquid courage", "grappa"],
                       "coffee": ["coffee", "espresso", "cafe latte", "Blend 43"],
                       "vodka" : ["Stolichnaya", "Absolut", "Grey Goose", "Ketel One", "Belvedere", "Luksusowa", "SKYY", "Finlandia", "Smirnoff"],
                       "whiskey":["Irish", "Jack Daniels", "Evan Williams", "Crown Royal", "Crown Royal Reserve", "Johnnie Walker Black", "Johnnie Walker Red", "Johnnie Walker Blue"],
                       "rum"   : ["Bundy", "Jamaican", "white", "dark", "spiced"],
                       "fictional": ["Romulan ale", "Blood wine", "Kanar", "Pan Galactic Gargle Blaster", "jynnan tonyx", "gee-N'N-T'N-ix", "jinond-o-nicks", "chinanto/mnigs", "tzjin-anthony-ks", "Moloko Plus", "Duff beer", "Panther Pilsner beer", "Screaming Viking", "Blue milk", "Fizzy Bubblech", "Butterbeer", "Ent-draught", "Nectar of the Gods", "Frobscottle"],
                       "tequila":["blanco", "oro", "reposado", "añejo", "extra añejo", "Patron Silver", "Jose Cuervo 1800"],
                       "scotch": ["single malt", "single grain", "blended malt", "blended grain", "blended", "Glenfiddich", "Glenlivet", "Dalwhinnie"],
                       "junk"  : ["blended kale", "pickle juice", "poorly-distilled rocket fuel", "caustic gas", "liquid smoke", "protein shake", "wheatgrass nonsense", "olive oil", "saline solution", "napalm", "synovial fluid", "drool"]},
            "prepared":["brewed", "distilled", "fermented", "decanted", "prayed over", "replicated", "conjured"],
            "degrees" :{"Kelvin": [0, 500], "degrees Celsius": [-20,95], "degrees Fahrenheit": [-20,200]}, #sane-ish ranges
            "suppress": ["coffee", "junk", "booze", "potion", "fictional"] } # do not append these to the random description


    def doTea(self, sender, replyto, msgwords):
        if len(msgwords) > 1: target = msgwords[1]
        else: target = sender
        drink = random.choice([msgwords[0]] * 50 + self.bev["drink"].keys())
        for vchoice in xrange(10):
            vessel = random.choice(self.bev["vessel"]["all"])
            if drink not in self.bev["vessel"].keys(): break # anything goes for these
            if vessel in self.bev["vessel"][drink]: break # match!
        fulldrink = random.choice(self.bev["drink"][drink])
        if drink not in self.bev["suppress"]: fulldrink += " " + drink
        tempunit = random.choice(self.bev["degrees"].keys())
        [tmin,tmax] = self.bev["degrees"][tempunit]
        temp = random.randrange(tmin,tmax)
        self.describeLog(replyto, random.choice(self.bev["serves"]) + " " + target
                + " a "  + vessel
                + " of " + fulldrink
                + ", "   + random.choice(self.bev["prepared"])
                + " by " + random.choice(self.brethren)
                + " at " + str(temp)
                + " " + tempunit + ".")

    def takeMessage(self, sender, replyto, msgwords):
        rcpt = msgwords[1].split(":")[0] # remove any trailing colon - could check for other things here.
        if (replyto == sender): #this was a privmsg
            forwardto = rcpt # so we pass a privmsg
        else: # !tell on channel
            forwardto = replyto # so pass to channel
        if not self.tellbuf.get(rcpt.lower(),False):
            self.tellbuf[rcpt.lower()] = []
        self.tellbuf[rcpt.lower()].append((forwardto,sender,time.time()," ".join(msgwords[2:])))
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
        for (forwardto,sender,ts,message) in self.tellbuf[plainuser]:
            self.respond(forwardto, user, "Message from " + sender + " at " + self.msgTime(ts) + ": " + message)
        del self.tellbuf[plainuser]
        self.tellbuf.sync()

    def forwardQuery(self,sender,replyto,msgwords):
        # need to pass the sender through to slaves so we can tag the response when it comes back
        # we call this the replytag
        # eg, bob says "!players" on the channel.
        # master sends privmsg to slave: "players #bob"
        # slave responds with "[hdf-eu] [gh] mike [nd] maryjane #bob"
        # master strips #bob from end of reply, and forwards to channel:
        # bob: [hdf-eu] [gh] mike [nd] maryjane
        # if bob sends private message, replytag is %bob instead.
        # master will then forward response direct to bob privately.
        if replyto == sender: # was a private message
            tag = "%"
        else:
            tag = "#"
        message = " ".join(msgwords + [tag + sender])
        for sl in self.slaves:
            self.msg(sl,message)


    def doPlayers(self,sender,replyto, msgwords):
        if self.slaves:
            self.forwardQuery(sender,replyto,msgwords)
        replytag = ""
        if SLAVE:
            replytag = " " + msgwords[-1]

        plrvar = ""
        for var in self.inprog.keys():
            for inpfile in glob.iglob(self.inprog[var] + "*.ttyrec"):
                # /stuff/crap/PLAYER:shit:garbage.ttyrec
                # we want AFTER last '/', BEFORE 1st ':'
                plrvar += inpfile.split("/")[-1].split(":")[0] + " " + self.displaytag(var) + " "
        if len(plrvar) == 0:
            plrvar = "No current players"
        self.respond(replyto, sender, self.displaytag(SERVERTAG) + " " + plrvar + replytag)


    def doWhereIs(self,sender,replyto, msgwords):
        replytag = ""
        minlen = 2
        if SLAVE:
            replytag = " " + msgwords[-1]
            minlen = 3
        if (len(msgwords) < minlen):
            #self.doPlayers(sender,replyto,msgwords)
            self.respond(replyto, sender, "!" + msgwords[0] + " <player> - finds a player in the dungeon." + replytag)
            return
        if self.slaves:
            self.forwardQuery(sender,replyto,msgwords)
        found = False
        ammy = ["", " (with Amulet)"]
        for var in self.whereis.keys():
            for wipath in glob.iglob(self.whereis[var] + "*.whereis"):
                if wipath.split("/")[-1].lower() == (msgwords[1] + ".whereis").lower():
                    found = True
                    plr = wipath.split("/")[-1].split(".")[0] # Correct case
                    wirec = parse_xlogfile_line(open(wipath, "r").read().strip(),":")

                    self.respond(replyto, sender, self.displaytag(SERVERTAG) + " " + plr
                                 + " "+self.displaytag(var)+": ({role} {race} {gender} {align}) T:{turns} ".format(**wirec)
                                 + self.dungeons[var][wirec["dnum"]]
                                 + " level: " + str(wirec["depth"])
                                 + ammy[wirec["amulet"]]
                                 + replytag)
        if not found:
            # Look for inprogress in case player is playing something that does not do whereis
            for var in self.inprog.keys():
                for inpfile in glob.iglob(self.inprog[var] + "*.ttyrec"):
                    plr = inpfile.split("/")[-1].split(":")[0]
                    if plr.lower() == msgwords[1].lower():
                        found = True
                        self.respond(replyto, sender, self.displaytag(SERVERTAG)
                                                      + " " + plr + " "
                                                      + self.displaytag(var)
                                                      + ": No details available"
                                                      + replytag)
            if not found and not SLAVE:
                self.respond(replyto, sender, self.displaytag(SERVERTAG) + " "
                                              + msgwords[1]
                                              + " is not currently playing on this server.")


    def plrVar(self, sender, replyto, msgwords):
        # for !streak and !asc, work out what player and variant they want
        if len(msgwords) > 3:
            # !streak tom dick harry
            self.respond(replyto,sender,"Usage: !" +msgwords[0] +" [variant] [player]")
            return
        if len(msgwords) == 3:
            vp = self.varalias(msgwords[1])
            pv = self.varalias(msgwords[2])
            if vp in self.variants.keys():
                # !streak dnh Tangles
                return (msgwords[2], vp)
            if pv in self.variants.keys():
                # !streak K2 UnNethHack
                return (msgwords[1],pv)
            # !streak bogus garbage
            self.respond(replyto,sender,"Usage: !" +msgwords[0] +" [variant] [player]")
            return (None, None)
        if len(msgwords) == 2:
            vp = self.varalias(msgwords[1])
            if vp in self.variants.keys():
                # !streak Grunthack
                return (sender, vp)
            # !streak Grasshopper
            return (msgwords[1],None)
        #!streak ...player is self, no variant
        return(sender, None)

    def doAsc(self, sender, replyto, msgwords):
        (PLR, var) = self.plrVar(sender,replyto,msgwords)
        if not PLR: return # bogus input, handled in plrVar
        plr = PLR.lower()
        stats = ""
        totasc = 0
        if var:
            if not plr in self.asc[var]:
                repl = "No ascensions for " + PLR + " in "
                if plr in self.allgames[var]:
                    repl += str(self.allgames[var][plr]) + " games of "
                repl += self.variants[var][0][0] + "."
                self.respond(replyto,sender,repl)
                return
            for role in self.variants[var][1]:
                role = role.title() # capitalise the first letter
                if role in self.asc[var][plr]:
                    totasc += self.asc[var][plr][role]
                    stats += " " + str(self.asc[var][plr][role]) + "x" + role
            stats += ", "
            for race in self.variants[var][2]:
                race = race.title()
                if race in self.asc[var][plr]:
                    stats += " " + str(self.asc[var][plr][race]) + "x" + race
            stats += ", "
            for alig in self.aligns:
                if alig in self.asc[var][plr]:
                    stats += " " + str(self.asc[var][plr][alig]) + "x" + alig
            stats += ", "
            for gend in self.genders:
                if gend in self.asc[var][plr]:
                    stats += " " + str(self.asc[var][plr][gend]) + "x" + gend
            stats += "."
            self.respond(replyto, sender,
                         PLR + " has ascended " + self.variants[var][0][0] + " "
                             + str(totasc) + " times in "
                             + str(self.allgames[var][plr])
                             + " games ({:0.2f}%):".format((100.0 * totasc)
                                                           / self.allgames[var][plr])
                             + stats)
            return
        # no variant. Do player stats across variants.
        totgames = 0
        for var in self.asc:
            totgames += self.allgames[var].get(plr,0)
            if plr in self.asc[var]:
                varasc = self.asc[var][plr].get("Mal",0)
                varasc += self.asc[var][plr].get("Fem",0)
                totasc += varasc
                if stats: stats += ","
                stats += " " + self.displaystring[var] + ":" + str(varasc) + " ({:0.2f}%)".format((100.0 * varasc)
                                                                                             / self.allgames[var][plr])
        if totasc:
            self.respond(replyto, sender,
                         PLR + " has ascended " + str(totasc) + " times in "
                             + str(totgames)
                             + " games ({:0.2f}%): ".format((100.0 * totasc) / totgames)
                             + stats)
            return
        if totgames:
            self.respond(replyto, sender, PLR + " has not ascended in " + str(totgames) + " games.")
            return
        self.respond(replyto, sender, "No games for " + PLR + ".")
        return

    def streakDate(self,stamp):
        return datetime.datetime.fromtimestamp(float(stamp)).strftime("%Y-%m-%d")

    def doStreak(self, sender, replyto, msgwords):
        (PLR, var) = self.plrVar(sender,replyto,msgwords)
        if not PLR: return # bogus input, handled in plrVar
        plr = PLR.lower()
        if var:
            if var not in self.streakvars:
                self.respond(replyto,sender,"Streaks are not recorded for " + var +".")
                return
            (lstart,lend,llength) = self.longstreak[var].get(plr,(0,0,0))
            (cstart,cend,clength) = self.curstreak[var].get(plr,(0,0,0))
            reply = PLR + "[" + self.displaystring[var] + "]"
            if llength == 0:
                reply += ": No streaks."
                self.respond(replyto,sender,reply)
                return
            reply += " Max: " + str(llength) + " (" + self.streakDate(lstart) \
                              + " - " + self.streakDate(lend) + ")"
            if clength > 0:
                if cstart == lstart:
                    reply += "(current)"
                else:
                    reply += ". Current: " + str(clength) + " (since " \
                                           + self.streakDate(cstart) + ")"
            reply += "."
            self.respond(replyto,sender,reply)
            return
        (lmax,cmax) = (0,0)
        for var in self.streakvars:
            (lstart,lend,llength) = self.longstreak[var].get(plr,(0,0,0))
            (cstart,cend,clength) = self.curstreak[var].get(plr,(0,0,0))
            if llength > lmax:
                (lmax, lvar, lsmax, lemax)  = (llength, var, lstart, lend)
            if clength > cmax:
                (cmax, cvar, csmax, cemax)  = (clength, var, cstart, cend)
        if lmax == 0:
            self.respond(replyto,sender, "No streaks for " + PLR +".")
            return
        reply = PLR + " Max[" + self.displaystring[lvar] + "]: " + str(lmax)
        reply += " (" + self.streakDate(lsmax) \
                      + " - " + self.streakDate(lemax) + ")"
        if cmax > 0:
            if csmax == lsmax:
                reply += "(current)"
            else:
                reply += ". Current[" + self.displaystring[cvar] + "]: " + str(cmax)
                reply += " (since " + self.streakDate(csmax) + ")"
        reply += "."
        self.respond(replyto,sender, reply)

    def lastGame(self, sender, replyto, msgwords):
        replytag = ""
        minlen = 3
        if SLAVE:
            replytag = " " + msgwords[-1]
            minlen = 4
        if self.slaves:
            self.forwardQuery(sender,replyto,msgwords)

        if (len(msgwords) >= minlen): #var, plr, any order.
            vp = self.varalias(msgwords[1])
            pv = self.varalias(msgwords[2])
            dl = self.lg.get(":".join([vp,pv]).lower(), False)
            if not dl:
                dl = self.lg.get(":".join([pv,vp]).lower(),False)
            if not dl:
                if not slave:
                    self.respond(replyto, sender, self.displaytag(SERVERTAG) +
                                 " No last game for (" + ",".join(msgwords[1:3]) + ") on this server.")
                return
            self.respond(replyto, sender, self.displaytag(SERVERTAG) + " " + dl + replytag)
            return
        if (len(msgwords) == minlen -1): #var OR plr - don't care which
            vp = self.varalias(msgwords[1])
            dl = self.lg.get(vp,False)
            if not dl:
                if not SLAVE:
                    self.respond(replyto, sender, self.displaytag(SERVERTAG) +
                                " No last game for " + msgwords[1] + " on this server.")
                return
            self.respond(replyto, sender, self.displaytag(SERVERTAG) + " " + dl + replytag)
            return
        self.respond(replyto, sender, self.displaytag(SERVERTAG) + " " + self.lastgame + replytag)

    def lastAsc(self, sender, replyto, msgwords):
        replytag = ""
        minlen = 3
        if SLAVE:
            replytag = " " + msgwords[-1]
            minlen = 4
        if self.slaves:
            self.forwardQuery(sender,replyto,msgwords)

        if (len(msgwords) >= minlen): #var, plr, any order.
            vp = self.varalias(msgwords[1])
            pv = self.varalias(msgwords[2])
            dl = self.la.get(":".join(pv,vp).lower(),False)
            if not dl:
                dl = self.la.get(":".join(vp,pv).lower(),False)
            if not dl:
                if not SLAVE:
                    self.respond(replyto, sender, self.displaytag(SERVERTAG) +
                                 " No last ascension for (" + ",".join(msgwords[1:3]) + ") on this server.")
                return
            self.respond(replyto, sender, self.displaytag(SERVERTAG) + " " + dl + replytag)
            return
        if (len(msgwords) == 2): #var OR plr - don't care which
            vp = self.varalias(msgwords[1])
            dl = self.la.get(vp,False)
            if not dl:
                if not SLAVE:
                    self.respond(replyto, sender, self.displaytag(SERVERTAG) +
                                 " No last ascension for " + msgwords[1] + " on this server.")
                return
            self.respond(replyto, sender, self.displaytag(SERVERTAG) + " " + dl + replytag)
            return
        self.respond(replyto, sender, self.displaytag(SERVERTAG) + " " + self.lastasc + replytag)

    # Allows players to set minimum turncount of their games to be reported
    # so they can manage their own deathspam
    # turncount may not be the best metric for this - open to suggestions
    # player name must match nick, or can be set by an admin.
    def setPlrTC(self, sender, replyto, msgwords):
        if SLAVE and not sender in MASTERS: return
        # set on all servers
        if self.slaves:
            self.forwardQuery(sender,replyto,msgwords)
        if SLAVE:
            sender = msgwords[-1][1:] # use the replytag
            msgwords = msgwords[0:-1] # strip the replytag - we're not sending anything back anyway
        if len(msgwords) == 2:
            if re.match(r'^\d+$',msgwords[1]):
                self.plr_tc[sender.lower()] = int(msgwords[1])
                self.plr_tc.sync()
                if not SLAVE: self.respond(replyto, sender, "Min reported turncount for " + sender.lower()
                                              + " set to " + msgwords[1])
                return
        if len(msgwords) == 1:
            if sender.lower() in self.plr_tc.keys():
                del self.plr_tc[sender.lower()]
                self.plr_tc.sync()
                if not SLAVE: self.respond(replyto, sender, "Min reported turncount for " + sender.lower()
                                              + " removed.")
                return
        if sender in self.admin:
            if len(msgwords) == 3:
                if re.match(r'^\d+$',msgwords[2]):
                    self.plr_tc[msgwords[1].lower()] = int(msgwords[2])
                    self.plr_tc.sync()
                    if not SLAVE: self.respond(replyto, sender, "Min reported turncount for " + msgwords[1].lower()
                                                  + " set to " + msgwords[2])
                    return
            if len(msgwords) == 2:
                if msgwords[1].lower() in self.plr_tc.keys():
                    del self.plr_tc[msgwords[1].lower()]
                    self.plr_tc.sync()
                    if not SLAVE: self.respond(replyto, sender, "Min reported turncount for " + msgwords[1].lower()
                                                 + " removed.")
                else:
                    if not SLAVE: self.respond(replyto, sender, "No min turncount for " + msgwords[1].lower())
                return
        else:
            if not SLAVE: self.respond(replyto, sender, "Usage: !" + msgwords[0] + " [turncount]")


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
        if dest != CHANNEL and sender in self.slaves: # response to slave query, or game announcement
            #msgwords = ["[" + self.slaves[sender] + "]"] + msgwords
            #queries need the response tag used and stripped
            if msgwords[-1][0] == '%': # response to private query
                sender = msgwords[-1][1:]
                self.respond(sender, sender, " ".join(msgwords[0:-1]))
                return
            if msgwords[-1][0] == '#': # resp to public query
                sender = msgwords[-1][1:]
                self.respond(CHANNEL, sender, " ".join(msgwords[0:-1]))
                return
            # game announcement, just throw it out there
            self.msg(CHANNEL, " ".join(msgwords))
        if re.match(r'^\d*d\d*$', msgwords[0]):
            self.rollDice(sender, replyto, msgwords)
            return
        if self.commands.get(msgwords[0].lower(), False):
            self.commands[msgwords[0].lower()](sender, replyto, msgwords)

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

    # players can request that their deaths not be reported if less than x turns
    def plr_tc_notreached(self, game):
        return (game["death"][0:8] not in ("ascended") #report these anyway!
           and game["name"].lower() in self.plr_tc.keys()
           and game["turns"] < self.plr_tc[game["name"].lower()])

    def xlogfileReport(self, game, report = True):
        var = game["variant"] # Make code less ugly
        # lowercased name is used for lookups
        lname = game["name"].lower()
        # "allgames" for a player even counts scummed games
        if not lname in self.allgames[var]:
            self.allgames[var][lname] = 0
        self.allgames[var][lname] += 1
        if self.startscummed(game): return

        dumplog = game.get("dumplog",False)
        if dumplog and var != "dyn":
            game["dumplog"] = fixdump(dumplog)
        # Need to figure out the dump path before messing with the name below
        dumpfile = (self.dump_file_prefix + game["dumpfmt"]).format(**game)
        dumpurl = "(sorry, no dump exists for {variant}:{name})".format(**game)
        if TEST or os.path.exists(dumpfile): # dump files may not exist on test system
            # quote only the game-specific part, not the prefix.
            # Otherwise it quotes the : in https://
            # assume the rest of the url prefix is safe.
            dumpurl = urllib.quote(game["dumpfmt"].format(**game))
            dumpurl = self.dump_url_prefix.format(**game) + dumpurl
        self.lg["{variant}:{name}".format(**game).lower()] = dumpurl
        if (game["endtime"] > self.lge.get(lname, 0)):
            self.lge[lname] = game["endtime"]
            self.lg[lname] = dumpurl
        self.lg[var] = dumpurl
        if (game["endtime"] > self.tlastgame):
            self.lastgame = dumpurl
            self.tlastgame = game["endtime"]

        if game["death"][0:8] in ("ascended"):
            # append dump url to report for ascensions
            game["ascsuff"] = "\n" + dumpurl
            # !lastasc stats.
            self.la["{variant}:{name}".format(**game).lower()] = dumpurl
            if (game["endtime"] > self.lae.get(lname, 0)):
                self.lae[lname] = game["endtime"]
                self.la[lname] = dumpurl
            self.la[var] = dumpurl
            if (game["endtime"] > self.tlastasc):
                self.lastasc = dumpurl
                self.tlastasc = game["endtime"]

            # !asc stats
            if not lname in self.asc[var]: self.asc[var][lname] = {}
            if not game["role"]   in self.asc[var][lname]: self.asc[var][lname][game["role"]]   = 0
            if not game["race"]   in self.asc[var][lname]: self.asc[var][lname][game["race"]]   = 0
            if not game["gender"] in self.asc[var][lname]: self.asc[var][lname][game["gender"]] = 0
            if not game["align"]  in self.asc[var][lname]: self.asc[var][lname][game["align"]]  = 0
            self.asc[var][lname][game["role"]]   += 1
            self.asc[var][lname][game["race"]]   += 1
            self.asc[var][lname][game["gender"]] += 1
            self.asc[var][lname][game["align"]]  += 1

            # streaks
            if var in self.streakvars:
                (cs_start, cs_end,
                 cs_length) = self.curstreak[var].get(lname,
                                                      (game["starttime"],0,0))
                cs_end = game["endtime"]
                cs_length += 1
                self.curstreak[var][lname] = (cs_start, cs_end, cs_length)
                (ls_start, ls_end,
                 ls_length) = self.longstreak[var].get(lname, (0,0,0))
                if cs_length > ls_length:
                    self.longstreak[var][lname] = self.curstreak[var][lname]

        else:   # not ascended - kill off any streak
            game["ascsuff"] = ""
            if var in self.streakvars:
                if lname in self.curstreak[var]:
                    del self.curstreak[var][lname]
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
            yield ("[{displaystring}] {name} ({role} {race} {gender} {align}), "
                   "{points} points, T:{turns}, {death}{ascsuff}").format(**game)
        else:
            if "modes" in game:
                if game["modes"].startswith("normal,"):
                    game["mode"] = game["modes"][7:]
                else:
                    game["mode"] = game["modes"]
            yield ("[{displaystring}] {name} ({role} {race} {gender} {align}), "
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
            yield ("[{displaystring}] {player} ({role} {race} {gender} {align}) "
                   "{message}, on T:{turns}").format(**event)
        elif "wish" in event:
            yield ("[{displaystring}] {player} ({role} {race} {gender} {align}) "
                   'wished for "{wish}", on T:{turns}').format(**event)
        elif "shout" in event:
            yield ("[{displaystring}] {player} ({role} {race} {gender} {align}) "
                   'shouted "{shout}", on T:{turns}').format(**event)
        elif "bones_killed" in event:
            yield ("[{displaystring}] {player} ({role} {race} {gender} {align}) "
                   "killed the {bones_monst} of {bones_killed}, "
                   "the former {bones_rank}, on T:{turns}").format(**event)
        elif "killed_uniq" in event:
            yield ("[{displaystring}] {player} ({role} {race} {gender} {align}) "
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
                game["displaystring"] = self.displaystring.get(game["variant"],game["variant"])
                game["dumpfmt"] = self.logs[filepath][3]
                for line in self.logs[filepath][0](game):
                    line = self.displaytag(SERVERTAG) + " " + line
                    if SLAVE:
                        for master in MASTERS:
                            self.msg(master, line)
                    else:
                        self.msgLog(CHANNEL, line)
                    for fwd in self.forwards[game["variant"]]:
                        self.msg(fwd, line)

            self.logs_seek[filepath] = handle.tell()

if __name__ == "__builtin__":
    f = protocol.ReconnectingClientFactory()
    f.protocol = DeathBotProtocol
    application = service.Application("DeathBot")
    deathservice = internet.SSLClient(HOST, PORT, f,
                                      ssl.ClientContextFactory())
    deathservice.setServiceParent(application)
