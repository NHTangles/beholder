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
#from twisted.words.protocols.irc import attributes as A
import datetime # for timestamp stuff
import time     # for !time
import ast      # for conduct/achievement bitfields - not really used
import os       # for check path exists (dumplogs)
import re       # for hello, and other things.
import urllib   # for dealing with NH4 variants' #&$#@ spaces in filenames.
import shelve   # for perstistent !tell messages
import random   # for !rng and friends
import glob     # for matchning in !whereis

TEST= False
#TEST = True  # uncomment for testing

# fn
HOST, PORT = "chat.us.freenode.net", 6697
CHANNEL = "#hardfought"
NICK = "Beholder"
if TEST:
    CHANNEL = "#hfdev"
    NICK = "BeerHolder"
FILEROOT="/opt/nethack/hardfought.org/"
WEBROOT="https://www.hardfought.org/"

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
    username = "beholder"
    realname = "Beholder"
    admin = ["K2", "Tangles"]  # for plr_tc maintenance. NOT SECURE obviously.
    try:
        password = open("/opt/beholder/pw", "r").read().strip()
    except:
        pass
    if TEST: password = "NotTHEPassword"

    sourceURL = "https://github.com/NHTangles/beholder"
    versionName = "beholder.py"
    versionNum = "0.1"

    dump_url_prefix = WEBROOT + "userdata/{name[0]}/{name}/"
    dump_file_prefix = FILEROOT + "dgldir/userdata/{name[0]}/{name}/"
    
    scoresURL = WEBROOT + "nethack/scoreboard (HDF) or https://scoreboard.xd.cm (ALL)"
    rceditURL = WEBROOT + "nethack/rcedit"
    helpURL = WEBROOT + "nethack"

    xlogfiles = {filepath.FilePath(FILEROOT+"nh343/var/xlogfile"): ("nh", ":", "nh343/dumplog/{starttime}.nh343.txt"),
                 filepath.FilePath(FILEROOT+"nhdev/var/xlogfile"): ("nd", "\t", "nhdev/dumplog/{starttime}.nhdev.txt"),
                 filepath.FilePath(FILEROOT+"gh/var/xlogfile"): ("gh", ":", "gh/dumplog/{starttime}.gh.txt"),
                 filepath.FilePath(FILEROOT+"dnethackdir/xlogfile"): ("dnh", ":", "dnethack/dumplog/{starttime}.dnh.txt"),
                 filepath.FilePath(FILEROOT+"fiqhackdir/data/xlogfile"): ("fh", ":", "fiqhack/dumplog/{dumplog}"),
                 filepath.FilePath(FILEROOT+"dynahack/dynahack-data/var/xlogfile"): ("dyn", ":", "dynahack/dumplog/{dumplog}"),
                 filepath.FilePath(FILEROOT+"nh4dir/save/xlogfile"): ("nh4", ":", "nethack4/dumplog/{dumplog}"),
                 filepath.FilePath(FILEROOT+"fourkdir/save/xlogfile"): ("4k", "\t", "nhfourk/dumps/{dumplog}"),
                 filepath.FilePath(FILEROOT+"sporkhack/var/xlogfile"): ("sp", "\t", "sporkhack/dumplog/{starttime}.sp.txt"),
                 filepath.FilePath(FILEROOT+"un531/var/unnethack/xlogfile"): ("un", ":", "un531/dumplog/{starttime}.un531.txt.html")}
    livelogs  = {filepath.FilePath(FILEROOT+"nh343/var/livelog"): ("nh", ":"),
                 filepath.FilePath(FILEROOT+"nhdev/var/livelog"): ("nd", "\t"),
                 filepath.FilePath(FILEROOT+"gh/var/livelog"): ("gh", ":"),
                 filepath.FilePath(FILEROOT+"dnethackdir/livelog"): ("dnh", ":"),
                 filepath.FilePath(FILEROOT+"fourkdir/save/livelog"): ("4k", "\t"),
                 filepath.FilePath(FILEROOT+"fiqhackdir/data/livelog"): ("fh", ":"),
                 filepath.FilePath(FILEROOT+"sporkhack/var/livelog"): ("sp", "\t"),
                 filepath.FilePath(FILEROOT+"un531/var/unnethack/livelog"): ("un", ":")}

    # for displaying variants in colour
    displaystring = {"nh" : "\x0315nh\x03",
                     "nd" : "\x0307nd\x03",
                     "gh" : "\x0304gh\x03",
                    "dnh" : "\x0313dnh\x03",
                     "fh" : "\x0310fh\x03",
                    "dyn" : "\x0305dyn\x03",
                    "nh4" : "\x0306nh4\x03",
                     "4k" : "\x03114k\x03",
                     "sp" : "\x0302sp\x03",
                     "un" : "\x0308un\x03"}

    # for !who or !players or whatever we end up calling it
    # Reduce the repetitive crap
    DGLD=FILEROOT+"dgldir/"
    INPR=DGLD+"inprogress-"
    inprog = { "nh" : INPR+"nh343/",
               "nd" : INPR+"nhdev/",
               "gh" : INPR+"gh/",
               "un" : INPR+"un531/",
              "dnh" : INPR+"dnh/",
               "fh" : INPR+"fh/",
               "4k" : INPR+"4k/",
              "nh4" : INPR+"nh4/",
               "sp" : INPR+"sp/",
              "dyn" : INPR+"dyn/"}
               
    # for !whereis 
    # some of these don't exist yet, so paths may not be accurate
    whereis = {"nh": FILEROOT+"nh343/var/whereis/",
               "nd": FILEROOT+"nhdev/var/whereis/",
               "gh": FILEROOT+"gh/var/whereis/",
              "dnh": FILEROOT+"dnethackdir/whereis/",
               "fh": FILEROOT+"fiqhackdir/var/whereis/",
              "dyn": FILEROOT+"dynahack/dynahack-data/var/whereis/",
              "nh4": FILEROOT+"nh4dir/save/whereis/",
               "4k": FILEROOT+"fourkdir/save/whereis/",
               "sp": FILEROOT+"sporkhack/var/",
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
                       vanilla_roles, vanilla_races + ["gia", "scu", "syl"])}

    # variants which support streaks
    streakvars = ["nh", "nd", "gh", "dnh", "un", "sp"]
    #who is making tea? - bots of the nethack community who have influenced this project.
    brethren = ["Rodney", "Athame", "Arsinoe", "Izchak", "TheresaMayBot", "the late Pinobot", "Announcy", "demogorgon", "the /dev/null/oracle"]
    looping_calls = None


    def signedOn(self):
        self.factory.resetDelay()
        self.startHeartbeat()
        self.join(CHANNEL)
        # seed the evil bastard RNG
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

        # for !tell
        self.tellbuf = shelve.open("/opt/beholder/tellmsg.db", writeback=True)
        # for !setmintc
        self.plr_tc = shelve.open("/opt/beholder/plrtc.db", writeback=True)

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
        if (self.nickname != NICK):
            self.setNick(NICK)

    def nickChanged(self, nn):
        # catch successful changing of nick from above and identify with nickserv
        if TEST: self.msg("Tangles", "identify " + nn + " " + self.password)
        else: self.msg("NickServ", "identify " + nn + " " + self.password)

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

    # construct and send response.
    # replyto is channel, or private nick
    # sender is original sender of query
    def respond(self, replyto, sender, message):
        if (replyto.lower() == sender.lower()): #private
            self.msg(replyto, message)
        else: #channel - prepend "Nick: " to message
            self.msg(replyto, sender + ": " + message)

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
        self.respond(replyto, sender, "available commands are !help !ping !time !pom !hello !booze !beer !potion !tea !coffee !whiskey !vodka !rum !tequila !scotch !goat !lotg !d(1-1000) !(1-50)d(1-1000) !8ball !rng !role !race !variant !tell !source !lastgame !lastasc !streak !rcedit !scores !sb !setmintc !whereis !players !who !commands")

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
        self.msg(replyto, "Hello " + sender + ", Welcome to " + CHANNEL)

    def doLotg(self, sender, replyto, msgwords):
        if len(msgwords) > 1: target = " ".join(msgwords[1:])
        else: target = sender
        self.msg(replyto, "May the Luck of the Grasshopper be with you always, " + target + "!")

    def doGoat(self, sender, replyto, msgwords):
        act = random.choice(['kicks', 'rams', 'headbutts'])
        part = random.choice(['arse', 'nose', 'face', 'kneecap'])
        if len(msgwords) > 1:
            self.msg(replyto, sender + "'s goat runs up and " + act + " " + " ".join(msgwords[1:]) + " in the " + part + "! Baaaaaa!")
        else:
            self.msg(replyto, NICK + "'s goat runs up and " + act + " " + sender + " in the " + part + "! Baaaaaa!")

    def doRng(self, sender, replyto, msgwords):
        if len(msgwords) == 1:
            if (sender[0:11].lower()) == "grasshopper": # always troll the grasshopper
                self.msg(replyto, "The RNG only has eyes for you, " + sender)
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
        multiword = " ".join(msgwords[1:]).split('|')
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
        self.describe(replyto, random.choice(self.bev["serves"]) + " " + target
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
        self.msg(replyto,"Will do, " + sender + "!")

    def msgTime(self, stamp):
        # Timezone handling is not great, but the following seems to work.
        # assuming TZ has not changed between leaving & taking the message.
        return datetime.datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M") + time.strftime(" %Z")

    def checkMessages(self, user):
        # this runs every time someone speaks on the channel,
        # so return quickly if there's nothing to do
        if not self.tellbuf.get(user.lower(),False): return
        for (forwardto,sender,ts,message) in self.tellbuf[user.lower()]:
            self.respond(forwardto, user, "Message from " + sender + " at " + self.msgTime(ts) + ": " + message)
        del self.tellbuf[user.lower()]
        self.tellbuf.sync()

    def doPlayers(self,sender,replyto, msgwords):
        plrvar = ""
        for var in self.inprog.keys():
            for inpfile in glob.iglob(self.inprog[var] + "*.ttyrec"): 
                # /stuff/crap/PLAYER:shit:garbage.ttyrec
                # we want AFTER last '/', BEFORE 1st ':' 
                plrvar += inpfile.split("/")[-1].split(":")[0] + " [" + self.displaystring[var] + "] "
        if len(plrvar) == 0:
            plrvar = "No current players"
        self.respond(replyto, sender, plrvar)
                
            
    def doWhereIs(self,sender,replyto, msgwords):
        if (len(msgwords) < 2):
            self.doPlayers(sender,replyto,msgwords)
            return
        found = False
        ammy = ["", " (with Amulet)"]
        for var in self.whereis.keys():
            for wipath in glob.iglob(self.whereis[var] + "*.whereis"): 
                if wipath.split("/")[-1].lower() == (msgwords[1] + ".whereis").lower():
                    found = True
                    plr = wipath.split("/")[-1].split(".")[0] # Correct case 
                    wirec = parse_xlogfile_line(open(wipath, "r").read().strip(),":")
                
                    self.respond(replyto, sender, plr
                                 + " ["+self.displaystring[var]+"]: ({role} {race} {gender} {align}) T:{turns} ".format(**wirec)
                                 + self.dungeons[var][wirec["dnum"]]
                                 + " level: " + str(wirec["depth"])
                                 + ammy[wirec["amulet"]])
        if not found:
            self.respond(replyto, sender, msgwords[1] + " is not currently playing.") 
        
    

    def streakDate(self,stamp):
        return datetime.datetime.fromtimestamp(float(stamp)).strftime("%Y-%m-%d")

    def doStreak(self, sender, replyto, msgwords):
        PLR = sender
        plr = sender.lower()
        var = False
        if len(msgwords) > 3:
            # !streak tom dick harry
            self.respond(replyto,sender,"Usage: !" +msgwords[0] +" [variant] [player]") 
            return
        if len(msgwords) == 3:
            vp = self.varalias(msgwords[1])
            pv = self.varalias(msgwords[2])
            if vp in self.variants.keys():
                # !streak dnh Tangles
                var = vp
                plr = pv
                PLR = msgwords[2]
            elif pv in self.variants.keys():
                # !streak K2 UnNethHack
                var = pv
                plr = vp
                PLR = msgwords[1]
            else: 
                # !streak bogus garbage
                self.respond(replyto,sender,"Usage: !" +msgwords[0] +" [variant] [player]") 
                return
        if len(msgwords) == 2:
            vp = self.varalias(msgwords[1])
            if vp in self.variants.keys():
                # !streak Grunthack
                var = vp
            else:
                # !streak Grasshopper
                plr = vp
                PLR = msgwords[1]
        if var:
            if var not in self.streakvars:
                self.respond(replyto,sender,"Streaks are not recoreded for " + var +".")
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
        if (len(msgwords) >= 3): #var, plr, any order.
            vp = self.varalias(msgwords[1])
            pv = self.varalias(msgwords[2])
            #dl = self.lg.get(":".join(msgwords[1:3]).lower(), False)
            dl = self.lg.get(":".join([vp,pv]).lower(), False)
            if not dl:
                #dl = self.lg.get(":".join(msgwords[2:0:-1]).lower(),
                dl = self.lg.get(":".join([pv,vp]).lower(),
                                 "No last game for (" + ",".join(msgwords[1:3]) + ")")
            self.respond(replyto, sender, dl)
            return
        if (len(msgwords) == 2): #var OR plr - don't care which
            vp = self.varalias(msgwords[1])
            dl = self.lg.get(vp,"No last game for " + msgwords[1])
            self.respond(replyto, sender, dl)
            return
        self.respond(replyto, sender, self.lastgame)

    def lastAsc(self, sender, replyto, msgwords):
        if (len(msgwords) >= 3): #var, plr, any order.
            vp = self.varalias(msgwords[1])
            pv = self.varalias(msgwords[2])
            dl = self.la.get(":".join(pv,vp).lower(),False)
            if (dl == False):
                dl = self.la.get(":".join(vp,pv).lower(),
                                 "No last ascension for (" + ",".join(msgwords[1:3]) + ")")
            self.respond(replyto, sender, dl)
            return
        if (len(msgwords) == 2): #var OR plr - don't care which
            vp = self.varalias(msgwords[1])
            dl = self.la.get(vp,"No last ascension for " + msgwords[1])
            self.respond(replyto, sender, dl)
            return
        self.respond(replyto, sender, self.lastasc)

    # Allows players to set minimum turncount of their games to be reported
    # so they can manage their own deathspam
    # turncount may not be the best metric for this - open to suggestions
    # player name must match nick, or can be set by an admin.
    def setPlrTC(self, sender, replyto, msgwords):
        if len(msgwords) == 2:
            if re.match(r'^\d+$',msgwords[1]):
                self.plr_tc[sender.lower()] = int(msgwords[1])
                self.plr_tc.sync()
                self.respond(replyto, sender, "Min reported turncount for " + sender.lower()
                                              + " set to " + msgwords[1])
                return
        if len(msgwords) == 1:
            if sender.lower() in self.plr_tc.keys():
                del self.plr_tc[sender.lower()]
                self.plr_tc.sync()
                self.respond(replyto, sender, "Min reported turncount for " + sender.lower()
                                              + " removed.")
                return
        if sender in self.admin:
            if len(msgwords) == 3:
                if re.match(r'^\d+$',msgwords[2]):
                    self.plr_tc[msgwords[1].lower()] = int(msgwords[2])
                    self.plr_tc.sync()
                    self.respond(replyto, sender, "Min reported turncount for " + msgwords[1].lower()
                                                  + " set to " + msgwords[2])
                    return
            if len(msgwords) == 2:
                if msgwords[1].lower() in self.plr_tc.keys():
                    del self.plr_tc[msgwords[1].lower()]
                    self.plr_tc.sync()
                    self.respond(replyto, sender, "Min reported turncount for " + msgwords[1].lower()
                                                 + " removed.")
                else: self.respond(replyto, sender, "No min turncount for " + msgwords[1].lower())
                return
        else:
            self.respond(replyto, sender, "Usage: !" + msgwords[0] + " [turncount]")

    # Listen to the chatter
    def privmsg(self, sender, dest, message):
        sender = sender.partition("!")[0]
        if (dest == CHANNEL): #public message
            replyto = CHANNEL
        else: #private msg
            replyto = sender
        # Hello processing first.
        if re.match(r'^(hello|hi|hey|salut|hallo|guten tag|shalom|ciao|hola|aloha|bonjour|hei|gday|konnichiwa|nuqneh)[!?. ]*$', message.lower()):
            self.doHello(sender, replyto)
        # Message checks next.
        self.checkMessages(sender)
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


    def startscummed(self, game):
        return game["death"] in ("quit", "escaped") and game["points"] < 1000

    # players can request that their deaths not be reported if less than x turns
    def plr_tc_notreached(self, game):
        return (game["death"][0:8] not in ("ascended") #report these anyway!
           and game["name"].lower() in self.plr_tc.keys()
           and game["turns"] < self.plr_tc[game["name"].lower()])

    def xlogfileReport(self, game, report = True):
        if self.startscummed(game): return

        dumplog = game.get("dumplog",False)
        if dumplog and game["variant"] != "dyn":
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
        if (game["endtime"] > self.lge.get(game["name"].lower(), 0)):
            self.lge[game["name"].lower()] = game["endtime"]
            self.lg[game["name"].lower()] = dumpurl
        self.lg[game["variant"].lower()] = dumpurl
        if (game["endtime"] > self.tlastgame):
            self.lastgame = dumpurl
            self.tlastgame = game["endtime"]
        if game["death"][0:8] in ("ascended"):
            game["ascsuff"] = "\n" + dumpurl
            self.la["{variant}:{name}".format(**game).lower()] = dumpurl
            if (game["endtime"] > self.lae.get(game["name"].lower(), 0)):
                self.lae[game["name"].lower()] = game["endtime"]
                self.la[game["name"].lower()] = dumpurl
            self.la[game["variant"].lower()] = dumpurl
            if (game["endtime"] > self.tlastasc):
                self.lastasc = dumpurl
                self.tlastasc = game["endtime"]
            if game["variant"] in self.streakvars:
                (cs_start, cs_end,
                 cs_length) = self.curstreak[game["variant"]].get(game["name"].lower(),
                                                         (game["starttime"],0,0))
                cs_end = game["endtime"]
                cs_length += 1
                self.curstreak[game["variant"]][game["name"].lower()] = (cs_start,
                                                                         cs_end,
                                                                         cs_length)
                (ls_start, ls_end,
                 ls_length) = self.longstreak[game["variant"]].get(game["name"].lower(),
                                                                   (0,0,0))
                if cs_length > ls_length:
                    self.longstreak[game["variant"]][game["name"].lower()] = self.curstreak[game["variant"]][game["name"].lower()]
        else:
            game["ascsuff"] = ""
            if game["variant"] in self.streakvars:
                if game["name"].lower() in self.curstreak[game["variant"]]:
                    del self.curstreak[game["variant"]][game["name"].lower()] 

        if (not report): return
        if self.plr_tc_notreached(game): return

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
                    self.say(CHANNEL, line)

            self.logs_seek[filepath] = handle.tell()

if __name__ == "__builtin__":
    f = protocol.ReconnectingClientFactory()
    f.protocol = DeathBotProtocol
    application = service.Application("DeathBot")
    deathservice = internet.SSLClient(HOST, PORT, f,
                                      ssl.ClientContextFactory())
    deathservice.setServiceParent(application)
