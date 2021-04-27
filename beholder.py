#!/usr/bin/python
# -*- coding: UTF-8 -*-
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
from twisted.internet.protocol import Protocol, ReconnectingClientFactory
from twisted.words.protocols import irc
from twisted.python import filepath, log
from twisted.python.logfile import DailyLogFile
from twisted.application import internet, service
import site     # to help find botconf
import base64   # for sasl login
import sys      # for logging something4
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

site.addsitedir('.')
from botconf import HOST, PORT, CHANNEL, NICK, USERNAME, REALNAME, BOTDIR
from botconf import PWFILE, FILEROOT, WEBROOT, LOGROOT, PINOBOT, ADMIN
from botconf import SERVERTAG
#from botconf import HOST, PORT, CHANNEL
#from botconf import NICK, USERNAME, REALNAME
#from botconf import PWFILE, LOGDIR, PINOBOT, ADMIN

try: from botconf import LOGBASE
except: LOGBASE = "/var/log/Beholder.log"
try: from botconf import LL_TURNCOUNTS
except: LL_TURNCOUNTS = {}
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
     "starttime", "curtime", "endtime", "user_seed",
     "uid", "turns", "xplevel", "exp","depth","dnum","score","amulet", "lltype"), int)
xlogfile_parse.update(dict.fromkeys(
    ("conduct", "event", "carried", "flags", "achieve"), ast.literal_eval))
#xlogfile_parse["starttime"] = fromtimestamp_int
#xlogfile_parse["curtime"] = fromtimestamp_int
#xlogfile_parse["endtime"] = fromtimestamp_int
xlogfile_parse["realtime"] = timedelta_int
#xlogfile_parse["deathdate"] = xlogfile_parse["birthdate"] = isodate
#xlogfile_parse["dumplog"] = fixdump

def parse_xlogfile_line(line, delim):
    record = {}
    for field in line.strip().decode(encoding='UTF-8', errors='ignore').split(delim):
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
    versionName = "beholder.py"
    versionNum = "0.1"

    dump_url_prefix = WEBROOT + "userdata/{name[0]}/{name}/"
    dump_file_prefix = FILEROOT + "dgldir/userdata/{name[0]}/{name}/"

    if not SLAVE:
        scoresURL = WEBROOT + "nethack/scoreboard (HDF) or https://nethackscoreboard.org (ALL)"
        rceditURL = WEBROOT + "nethack/rcedit"
        helpURL = WEBROOT + "nethack"
        logday = time.strftime("%d")
        chanLogName = LOGROOT + CHANNEL + time.strftime("-%Y-%m-%d.log")
        chanLog = open(chanLogName,'a')
        os.chmod(chanLogName,stat.S_IRUSR|stat.S_IWUSR|stat.S_IRGRP|stat.S_IROTH)

    xlogfiles = {filepath.FilePath(FILEROOT+"nh343-hdf/var/xlogfile"): ("nh343", ":", "nh343/dumplog/{starttime}.nh343.txt"),
                 filepath.FilePath(FILEROOT+"nh363-hdf/var/xlogfile"): ("nh363", "\t", "nethack/dumplog/{starttime}.nh.html"),
                 filepath.FilePath(FILEROOT+"nh370.32-hdf/var/xlogfile"): ("nh370", "\t", "nethack/dumplog/{starttime}.nh.html"),
                 filepath.FilePath(FILEROOT+"grunthack-0.2.4/var/xlogfile"): ("gh", ":", "gh/dumplog/{starttime}.gh.txt"),
                 filepath.FilePath(FILEROOT+"dnethack-3.20.0/xlogfile"): ("dnh", ":", "dnethack/dumplog/{starttime}.dnh.txt"),
                 filepath.FilePath(FILEROOT+"fiqhackdir/data/xlogfile"): ("fh", ":", "fiqhack/dumplog/{dumplog}"),
                 filepath.FilePath(FILEROOT+"dynahack/dynahack-data/var/xlogfile"): ("dyn", ":", "dynahack/dumplog/{dumplog}"),
                 filepath.FilePath(FILEROOT+"nh4dir/save/xlogfile"): ("nh4", ":", "nethack4/dumplog/{dumplog}"),
                 filepath.FilePath(FILEROOT+"fourkdir/save/xlogfile"): ("4k", "\t", "nhfourk/dumps/{dumplog}"),
                 filepath.FilePath(FILEROOT+"sporkhack-0.6.5/var/xlogfile"): ("sp", "\t", "sporkhack/dumplog/{starttime}.sp.txt"),
                 filepath.FilePath(FILEROOT+"slex-2.7.0/xlogfile"): ("slex", "\t", "slex/dumplog/{starttime}.slex.txt"),
                 filepath.FilePath(FILEROOT+"xnethack-6.0.0/var/xlogfile"): ("xnh", "\t", "xnethack/dumplog/{starttime}.xnh.html"),
                 filepath.FilePath(FILEROOT+"splicehack-0.8.2/var/xlogfile"): ("spl", "\t", "splicehack/dumplog/{starttime}.splice.html"),
                 filepath.FilePath(FILEROOT+"nh13d/xlogfile"): ("nh13d", ":", "nh13d/dumplog/{starttime}.nh13d.txt"),
                 filepath.FilePath(FILEROOT+"slashem-0.0.8E0F2/xlogfile"): ("slshm", ":", "slashem/dumplog/{starttime}.slashem.txt"),
                 filepath.FilePath(FILEROOT+"notdnethack-2020.04.16/xlogfile"): ("ndnh", ":", "notdnethack/dumplog/{starttime}.ndnh.txt"),
                 filepath.FilePath(FILEROOT+"evilhack-0.6.0/var/xlogfile"): ("evil", "\t", "evilhack/dumplog/{starttime}.evil.html"),
                 filepath.FilePath(FILEROOT+"setseed.32/var/xlogfile"): ("seed", "\t", "setseed/dumplog/{starttime}.seed.html"),
                 filepath.FilePath(FILEROOT+"slashthem-0.9.5/xlogfile"): ("slth", ":", "slashthem/dumplog/{starttime}.slth.txt"),
                 filepath.FilePath(FILEROOT+"unnethack-6.0.3/var/unnethack/xlogfile"): ("un", "\t", "unnethack/dumplog/{starttime}.un.txt.html")}
    livelogs  = {filepath.FilePath(FILEROOT+"nh343-hdf/var/livelog"): ("nh343", ":"),
                 filepath.FilePath(FILEROOT+"nh363-hdf/var/livelog"): ("nh363", "\t"),
                 filepath.FilePath(FILEROOT+"nh370.32-hdf/var/livelog"): ("nh370", "\t"),
                 filepath.FilePath(FILEROOT+"grunthack-0.2.4/var/livelog"): ("gh", ":"),
                 filepath.FilePath(FILEROOT+"dnethack-3.20.0/livelog"): ("dnh", ":"),
                 filepath.FilePath(FILEROOT+"fourkdir/save/livelog"): ("4k", "\t"),
                 filepath.FilePath(FILEROOT+"fiqhackdir/data/livelog"): ("fh", ":"),
                 filepath.FilePath(FILEROOT+"sporkhack-0.6.5/var/livelog"): ("sp", ":"),
                 filepath.FilePath(FILEROOT+"slex-2.7.0/livelog"): ("slex", ":"),
                 filepath.FilePath(FILEROOT+"xnethack-6.0.0/var/livelog"): ("xnh", "\t"),
                 filepath.FilePath(FILEROOT+"splicehack-0.8.2/var/livelog"): ("spl", "\t"),
                 filepath.FilePath(FILEROOT+"nh13d/livelog"): ("nh13d", ":"),
                 filepath.FilePath(FILEROOT+"slashem-0.0.8E0F2/livelog"): ("slshm", ":"),
                 filepath.FilePath(FILEROOT+"notdnethack-2020.04.16/livelog"): ("ndnh", ":"),
                 filepath.FilePath(FILEROOT+"evilhack-0.6.0/var/livelog"): ("evil", "\t"),
                 filepath.FilePath(FILEROOT+"setseed.32/var/livelog"): ("seed", "\t"),
                 filepath.FilePath(FILEROOT+"slashthem-0.9.5/livelog"): ("slth", ":"),
                 filepath.FilePath(FILEROOT+"unnethack-6.0.3/var/unnethack/livelog"): ("un", ":")}

    # Forward events to other bots at the request of maintainers of other variant-specific channels
    forwards = {"nh343" : [],
                "nh363" : [],
                "nh370" : [],
                 "zapm" : [],
                   "gh" : [],
                  "dnh" : [],
                   "fh" : ["Arsinoe"],
                  "dyn" : [],
                  "nh4" : ["Arsinoe"],
                   "4k" : ["Arsinoe"],
                   "sp" : [],
                 "slex" : ["ro-bot"],
                  "xnh" : [],
                  "spl" : [],
                "nh13d" : [],
                "slshm" : [],
                 "tnnt" : [],
                 "ndnh" : [],
                 "evil" : [],
                 "seed" : [],
                 "slth" : [],
                   "un" : []}

    # for displaying variants and server tags in colour
    displaystring = {"nh343" : "\x0315nh343\x03",
                     "nh363" : "\x0307nh363\x03",
                     "nh370" : "\x0307nh370\x03",
                      "zapm" : "\x0303zapm\x03",
                        "gh" : "\x0304gh\x03",
                       "dnh" : "\x0313dnh\x03",
                        "fh" : "\x0310fh\x03",
                       "dyn" : "\x0305dyn\x03",
                       "nh4" : "\x0306nh4\x03",
                        "4k" : "\x03114k\x03",
                        "sp" : "\x0314sp\x03",
                      "slex" : "\x0312slex\x03",
                       "xnh" : "\x0309xnh\x03",
                       "spl" : "\x0303spl\x03",
                     "nh13d" : "\x0311nh13d\x03",
                     "slshm" : "\x0314slshm\x03",
                      "ndnh" : "\x0313ndnh\x03",
                      "evil" : "\x0304evil\x03",
                      "tnnt" : "\x0310tnnt\x03",
                        "un" : "\x0308un\x03",
                      "seed" : "\x0315seed\x03",
                      "slth" : "\x0305slth\x03",
                    "hdf-us" : "\x1D\x0304hdf-us\x03\x0F",
                    "hdf-au" : "\x1D\x0303hdf-au\x03\x0F",
                    "hdf-eu" : "\x1D\x0312hdf-eu\x03\x0F"}

    # put the displaystring for a thing in square brackets
    def displaytag(self, thing):
       return '[' + self.displaystring.get(thing,thing) + ']'

    # for !who or !players or whatever we end up calling it
    # Reduce the repetitive crap
    DGLD=FILEROOT+"dgldir/"
    INPR=DGLD+"inprogress-"
    inprog = { "nh343" : [INPR+"nh343-hdf/"],
               "nh363" : [INPR+"nh363-hdf/"],
               "nh370" : [INPR+"nh370.16-hdf/", INPR+"nh370.17-hdf/",
                          INPR+"nh370.18-hdf/", INPR+"nh370.20-hdf/",
                          INPR+"nh370.22-hdf/", INPR+"nh370.23-hdf/",
                          INPR+"nh370.27-hdf/", INPR+"nh370.28-hdf/",
                          INPR+"nh370.29-hdf/", INPR+"nh370.30-hdf/",
                          INPR+"nh370.31-hdf/", INPR+"nh370.32-hdf/"],
                "zapm" : [INPR+"zapm/"],
                  "gh" : [INPR+"gh024/"],
                  "un" : [INPR+"un531/", INPR+"un532/",
                          INPR+"un600/", INPR+"un601/",
                          INPR+"un602/", INPR+"un603/"],
                 "dnh" : [INPR+"dnh3171/", INPR+"dnh318/",
                          INPR+"dnh319/", INPR+"dnh3191/",
                          INPR+"dnh320/"],
                  "fh" : [INPR+"fh/"],
                  "4k" : [INPR+"4k/"],
                 "nh4" : [INPR+"nh4/"],
                  "sp" : [INPR+"sp065/"],
                "slex" : [INPR+"slex250/", INPR+"slex260/",
                          INPR+"slex266/", INPR+"slex268/",
                          INPR+"slex270/"],
                 "xnh" : [INPR+"xnh040/", INPR+"xnh041/",
                          INPR+"xnh50/", INPR+"xnh51/",
                          INPR+"xnh51.1/", INPR+"xnh51.2/",
                          INPR+"xnh51.3/", INPR+"xnh600/"],
                 "spl" : [INPR+"spl063/", INPR+"spl064/",
                          INPR+"spl070/", INPR+"spl071/",
                          INPR+"spl071.21/", INPR+"spl080/",
                          INPR+"spl081/", INPR+"spl082/"],
               "nh13d" : [INPR+"nh13d/"],
               "slshm" : [INPR+"slashem/"],
                "ndnh" : [INPR+"ndnh-524/", INPR+"ndnh-1224/",
                          INPR+"ndnh-0416/"],
                "evil" : [INPR+"evil040/", INPR+"evil041/",
                          INPR+"evil042/", INPR+"evil050/",
                          INPR+"evil060/"],
                "tnnt" : [INPR+"tnnt/"],
                "seed" : [INPR+"seed/",
                          INPR+"seed.32/"],
                "slth" : [INPR+"slth095/"],
                 "dyn" : [INPR+"dyn/"]}

    # for !whereis
    whereis = {"nh343": [FILEROOT+"nh343-hdf/var/whereis/"],
               "nh363": [FILEROOT+"nh363-hdf/var/whereis/"],
               "nh370": [FILEROOT+"nh370.16-hdf/var/whereis/",
                         FILEROOT+"nh370.17-hdf/var/whereis/",
                         FILEROOT+"nh370.18-hdf/var/whereis/",
                         FILEROOT+"nh370.20-hdf/var/whereis/",
                         FILEROOT+"nh370.22-hdf/var/whereis/",
                         FILEROOT+"nh370.23-hdf/var/whereis/",
                         FILEROOT+"nh370.27-hdf/var/whereis/",
                         FILEROOT+"nh370.28-hdf/var/whereis/",
                         FILEROOT+"nh370.29-hdf/var/whereis/",
                         FILEROOT+"nh370.30-hdf/var/whereis/",
                         FILEROOT+"nh370.31-hdf/var/whereis/",
                         FILEROOT+"nh370.32-hdf/var/whereis/"],
                  "gh": [FILEROOT+"grunthack-0.2.4/var/whereis/"],
                 "dnh": [FILEROOT+"dnethack-3.17.1/whereis/",
                         FILEROOT+"dnethack-3.18.0/whereis/",
                         FILEROOT+"dnethack-3.19.0/whereis/",
                         FILEROOT+"dnethack-3.19.1/whereis/",
                         FILEROOT+"dnethack-3.20.0/whereis/"],
                  "fh": [FILEROOT+"fiqhackdir/data/"],
                 "dyn": [FILEROOT+"dynahack/dynahack-data/var/whereis/"],
                 "nh4": [FILEROOT+"nh4dir/save/whereis/"],
                  "4k": [FILEROOT+"fourkdir/save/"],
                  "sp": [FILEROOT+"sporkhack-0.6.5/var/"],
                "slex": [FILEROOT+"slex-2.5.0/whereis/",
                         FILEROOT+"slex-2.6.0/whereis/",
                         FILEROOT+"slex-2.6.6/whereis/",
                         FILEROOT+"slex-2.6.8/whereis/",
                         FILEROOT+"slex-2.7.0/whereis/"],
                 "xnh": [FILEROOT+"xnethack-0.4.0/var/whereis/",
                         FILEROOT+"xnethack-0.4.1/var/whereis/",
                         FILEROOT+"xnethack-5.0/var/whereis/",
                         FILEROOT+"xnethack-5.1/var/whereis/",
                         FILEROOT+"xnethack-5.1.1/var/whereis/",
                         FILEROOT+"xnethack-5.1.2/var/whereis/",
                         FILEROOT+"xnethack-5.1.3/var/whereis/",
                         FILEROOT+"xnethack-6.0.0/var/whereis/"],
                 "spl": [FILEROOT+"splicehack-0.6.3/var/whereis/",
                         FILEROOT+"splicehack-0.6.4/var/whereis/",
                         FILEROOT+"splicehack-0.7.0/var/whereis/",
                         FILEROOT+"splicehack-0.7.1/var/whereis/",
                         FILEROOT+"splicehack-0.7.1-21/var/whereis/",
                         FILEROOT+"splicehack-0.8.0/var/whereis/",
                         FILEROOT+"splicehack-0.8.1/var/whereis/",
                         FILEROOT+"splicehack-0.8.2/var/whereis/"],
               "nh13d": [FILEROOT+"nh13d/whereis/"],
               "slshm": [FILEROOT+"slashem-0.0.8E0F2/whereis/"],
                "ndnh": [FILEROOT+"notdnethack-2019.05.24/whereis/",
                         FILEROOT+"notdnethack-2019.12.24/whereis/",
                         FILEROOT+"notdnethack-2020.04.16/whereis/"],
                "evil": [FILEROOT+"evilhack-0.4.0/var/whereis/",
                         FILEROOT+"evilhack-0.4.1/var/whereis/",
                         FILEROOT+"evilhack-0.4.2/var/whereis/",
                         FILEROOT+"evilhack-0.5.0/var/whereis/",
                         FILEROOT+"evilhack-0.6.0/var/whereis/"],
                "tnnt": [FILEROOT+"tnnt/var/whereis/"],
                "seed": [FILEROOT+"setseed/var/whereis/",
                         FILEROOT+"setseed.32/var/whereis/"],
                "slth": [FILEROOT+"slashthem-0.9.5/whereis/"],
                  "un": [FILEROOT+"un531/var/unnethack/",
                         FILEROOT+"un532/var/unnethack/",
                         FILEROOT+"unnethack-6.0.0/var/unnethack/",
                         FILEROOT+"unnethack-6.0.1/var/unnethack/",
                         FILEROOT+"unnethack-6.0.2/var/unnethack/",
                         FILEROOT+"unnethack-6.0.3/var/unnethack/"]}

    dungeons = {"nh343": ["The Dungeons of Doom","Gehennom","The Gnomish Mines","The Quest",
                          "Sokoban","Fort Ludios","Vlad's Tower","The Elemental Planes"],
                "nh363": ["The Dungeons of Doom","Gehennom","The Gnomish Mines","The Quest",
                          "Sokoban","Fort Ludios","Vlad's Tower","The Elemental Planes"],
                "nh370": ["The Dungeons of Doom","Gehennom","The Gnomish Mines","The Quest",
                          "Sokoban","Fort Ludios","Vlad's Tower","The Elemental Planes"],
                   "gh": ["The Dungeons of Doom","Gehennom","The Gnomish Mines","The Quest",
                          "Sokoban","Fort Ludios","Vlad's Tower","The Elemental Planes"],
                  "dnh": ["The Dungeons of Doom","Gehennom","The Gnomish Mines","Law Quest",
                          "Neutral Quest","The Lost Cities","Chaos Quest","The Quest",
                          "Sokoban","Fort Ludios","The Lost Tomb","The Sunless Sea",
                          "The Temple of Moloch","The Dispensary","Vlad's Tower",
                          "The Elemental Planes"],
                 "ndnh": ["The Dungeons of Doom","Gehennom","The Gnomish Mines","The Ice Caves",
                          "The Black Forest","The Dismal Swamp","The Archipelago","Law Quest",
                          "Neutral Quest","The Lost Cities","Chaos Quest","The Quest",
                          "Lokoban","Fort Ludios","The Void","Sacristy","The Lost Tomb",
                          "The Sunless Sea","The Temple of Moloch","The Dispensary","The Spire",
                          "Vlad's Tower","The Elemental Planes"],
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
                          "The Subquest","Bell Caves","Rival Quest",
                          "Lawful Quest","Neutral Quest","Chaotic Quest",
                          "Sokoban","Town","Grund's Stronghold","Fort Ludios","The Wyrm Caves",
                          "The Ice Queen's Realm",
                          "One-eyed Sam's Market","The Lost Tomb","The Spider Caves","The Sunless Sea",
                          "The Temple of Moloch","Grue Challenge","Joust Challenge","Pacman Challenge",
                          "Pool Challenge","Digdug Challenge",
                          "Illusory Castle","Deep Mines","Space Base",
                          "Sewer Plant","Gamma Caves","Mainframe","Void","Nether Realm","Angmar",
                          "Emyn Luin","Minotaur Maze","Swimming Pool","Hell's Bathroom",
                          "Minus World","Green Cross",
                          "The Giant Caverns","Frankenstein's Lab",
                          "Sheol","Vlad's Tower","Yendorian Tower","Forging Chamber",
                          "Dead Grounds","Ordered Chaos",
                          "Resting Zone GA","Resting Zone GB","Resting Zone GC","Resting Zone GD",
                          "Resting Zone GE","Resting Zone TA","Resting Zone TB","Resting Zone TC",
                          "Resting Zone TD","Resting Zone TE","Resting Zone TF","Resting Zone TG",
                          "Resting Zone TH","Resting Zone TI","Resting Zone TJ","Resting Zone A",
                          "Resting Zone S",
                          "The Elemental Planes"],
                  "xnh": ["The Dungeons of Doom","Gehennom","The Gnomish Mines","The Quest",
                          "Sokoban","Fort Ludios","Vlad's Tower","The Elemental Planes"],
                  "spl": ["The Dungeons of Doom","The Void","The Icy Wastes","The Dark Forest",
                          "Mysterious Laboratory","Gehennom","The Gnomish Mines","Banquet Hall",
                          "The Quest","Sokoban","One-eyed Sam's Market",
                          "Fort Ludios","Vlad's Tower","The Elemental Planes"],
                "nh13d": ["The Dungeons of Doom"],
                "slshm": ["The Dungeons of Doom","Gehennom","The Gnomish Mines",
                          "The Quest","Sokoban","Town","Fort Ludios",
                          "One-eyed Sam's Market","Vlad's Tower","The Dragon Caves",
                          "The Elemental Planes"],
                 "slth": ["The Dungeons of Doom","Gehennom","The Gnomish Mines",
                          "The Quest","Lawful Quest","Neutral Quest","Chaotic Quest",
                          "Sokoban","Town","Grund's Stronghold","Fort Ludios","The Wyrm Caves",
                          "One-eyed Sam's Market","The Lost Tomb","The Spider Caves","The Sunless Sea",
                          "The Temple of Moloch","The Giant Caverns","Vlad's Tower","Frankenstein's Lab",
                          "The Elemental Planes"],
                 "tnnt": ["The Dungeons of Doom","Gehennom","The Gnomish Mines","The Quest",
                          "Sokoban","Fort Ludios","DevTeam's Office","Deathmatch Arena",
                          "Vlad's Tower","The Elemental Planes"],
                 "evil": ["The Dungeons of Doom","Gehennom","The Gnomish Mines","The Quest",
                          "Sokoban","Fort Ludios","The Ice Queen's Realm","Vlad's Tower",
                          "The Elemental Planes"],
                 "seed": ["The Dungeons of Doom","Gehennom","The Gnomish Mines","The Quest",
                          "Sokoban","Fort Ludios","Vlad's Tower","The Elemental Planes"],
                   "un": ["The Dungeons of Doom","Gehennom","Sheol","The Gnomish Mines",
                          "The Quest","Sokoban","Town","The Ruins of Moria","Fort Ludios",
                          "One-eyed Sam's Market","Vlad's Tower","The Dragon Caves",
                          "The Elemental Planes","Advent Calendar"]}

    # variant related stuff that does not relate to xlogfile processing
    rolename = 	{
        # Vanilla
        "arc": "archeologist",
        "bar": "barbarian",
        "cav": "caveman",
        "hea": "healer",
        "kni": "knight",
        "mon": "monk",
        "pri": "priest",
        "ran": "ranger",
        "rog": "rogue",
        "sam": "samurai",
        "tou": "tourist",
        "val": "valkyrie",
        "wiz": "wizard",
        # NetHack 1.3d Vanilla has a role of 'Elf' as well as 'Fighter' and 'Ninja' (the latter already included below from Slash'EM Extended roles)
        "elf": "elf",
        # Dnh, includes all of vanilla
        "ana": "anachrononaut",
        "bin": "binder",
        "nob": "noble",
        "pir": "pirate",
        "trb": "troubadour",
        "con": "convict",
        # Ndnh, includes all of vanilla and dnh
        "acu": "illithanachronounbinder",
        # SpliceHack, includes all of vanilla
        "car": "cartomancer",
        "dgn": "dragonmaster",
        "dan": "dancer",
        # Evilhack, includes all of vanilla
        "inf": "infidel",
        # SLEX, includes all of dnh and slash
        "act": "activistor",
        "alt": "altmer",
        "ama": "amazon",
        "art": "artist",
        "ass": "assassin",
        "ast": "astronaut",
        "aug": "augurer",
        "brd": "bard",
        "blo": "bloodseeker",
        "bos": "bosmer",
        "bul": "bully",
        "cam": "camperstriker",
        "cel": "cellar child",
        "cha": "chaos sorceror",
        "che": "chevalier",
        "coo": "cook",
        "cou": "courier",
        "cra": "cracker",
        "cru": "cruel abuser",
        "cyb": "cyber ninja",
        "dea": "death eater",
        "dem": "demagogue",
        "dis": "dissident",
        "div": "diver",
        "dol": "doll mistress",
        "doo": "doom marine",
        "dqs": "dq slime",
        "dri": "druid",
        "dru": "drunk",
        "dun": "dunmer",
        "elm": "electric mage",
        "ele": "elementalist",
        "elp": "elph",
        "emp": "empath",
        "erd": "erdrick",
        "fai": "failed existence",
        "fea": "feat master",
        "fem": "feminist",
        "fen": "fencer",
        "fig": "fighter",
        "fla": "flame mage",
        "for": "form changer",
        "fox": "foxhound agent",
        "gam": "gamer",
        "gan": "gang scholar",
        "gns": "gangster",
        "gee": "geek",
        "gla": "gladiator",
        "gof": "goff",
        "gol": "goldminer",
        "gra": "graduate",
        "gre": "grenadonin",
        "gun": "gunner",
        "ice": "ice mage",
        "int": "intel scribe",
        "jan": "janitor",
        "jed": "jedi",
        "jes": "jester",
        "jus": "justice keeper",
        "kor": "korsair",
        "kur": "kurwa",
        "lad": "ladiesman",
        "lib": "librarian",
        "loc": "locksmith",
        "lun": "lunatic",
        "mah": "mahou shoujo",
        "mam": "mastermind",
        "med": "medium",
        "mid": "midget",
        "mur": "murderer",
        "mus": "musician",
        "nec": "necromancer",
        "nin": "ninja",
        "nuc": "nuclear physicist",
        "occ": "occult master",
        "off": "officer",
        "ord": "ordinator",
        "pal": "paladin",
        "pic": "pickpocket",
        "poi": "poison mage",
        "pok": "pokemon",
        "pol": "politician",
        "pro": "prostitute",
        "psi": "psion",
        "psy": "psyker",
        "qua": "quarterback",
        "rin": "ringseeker",
        "roc": "rocker",
        "sag": "sage",
        "sai": "saiyan",
        "sci": "scientist",
        "sha": "shapeshifter",
        "sla": "slave master",
        "sma": "space marine",
        "soc": "social justice warrior",
        "sof": "software engineer",
        "spa": "spacewars fighter",
        "sto": "stormboy",
        "sup": "supermarket cashier",
        "tha": "thalmor",
        "top": "topmodel",
        "trc": "tracer",
        "trl": "transsylvanian",
        "trv": "transvestite",
        "twe": "twelph",
        "unb": "unbeliever",
        "uds": "undead slayer",
        "und": "undertaker",
        "use": "user of stand",
        "wal": "walscholar",
        "wan": "wandkeeper",
        "war": "warrior",
        "wei": "weirdboy",
        "xel": "xelnaga",
        "yau": "yautja",
        "yeo": "yeoman",
        "yse": "ysexymate",
        "zoo": "zookeeper",
        "zyb": "zyborg"
    }

    racename = {
        # Vanilla
        "dwa": "dwarf",
        "elf": "elf",
        "gno": "gnome",
        "hum": "human",
        "orc": "orc",
        # Grunt, includes vanilla
        "gia": "giant",
        "kob": "kobold",
        "ogr": "ogre",
        # Dnh, includes vanilla
        "clk": "clockwork automaton",
        "hlf": "half-dragon",
        "inc": "incantifier",
        "vam": "vampire",
        "yuk": "yuki-onna",
        "dro": "drow",
        "bat": "chiropteran",
        # 4k, includes vanilla
        "scu": "scurrier",
        "syl": "sylph",
        #SpliceHack, includes vanilla and one race from SLEX (Angel)
        "chg": "changeling",
        "inf": "infernal",
        "mer": "merfolk",
        "wlf": "werewolf",
        # SLEX, includes vanilla, grunt, 4k, slash and dnh -- not all of dnh but close
        "add": "addict",
        "agg": "aggravator",
        "akt": "ak thief is dead!",
        "alb": "albae",
        "alc": "alchemist",
        "ali": "alien",
        "ame": "american",
        "amn": "amnesiac",
        "anc": "ancient",
        "anp": "ancipital",
        "agb": "angbander",
        "ang": "angel",
        "aqu": "aquarian",
        "arg": "argonian",
        "asg": "asgardian",
        "asu": "asura",
        "atl": "atlantean",
        "aur": "golden saint",
        "azt": "aztpok",
        "bas": "bastard",
        "btm": "batman",
        "bor": "borg",
        "bre": "breton",
        "bur": "burninator",
        "cen": "centaur",
        "chi": "chiropteran",
        "chu": "chiquai",
        "coc": "cockatrice",
        "col": "colorator",
        "cor": "cortex",
        "cur": "curser",
        "dar": "dark seducer",
        "dea": "deathmold",
        "dee": "deep elf",
        "des": "destabilizer",
        "dev": "developer",
        "dvl": "devil",
        "din": "dinosaur",
        "dol": "dolgsman",
        "dop": "doppelganger",
        "dra": "dragon",
        "dry": "dryad",
        "duf": "dufflepud",
        "dun": "dunadan",
        "egy": "egymid",
        "ele": "elemental",
        "eng": "engchip",
        "ent": "ent",
        "evi": "evilvariant",
        "exp": "expert",
        "faw": "fawn",
        "fel": "felid",
        "fen": "fenek",
        "fie": "fiend",
        "fre": "frendian",
        "fro": "fro",
        "gas": "gastly",
        "gel": "gelatinous cube",
        "gol": "golem",
        "gre": "green slime",
        "grm": "gremlin",
        "gri": "grid bug",
        "gru": "greuro",
        "hax": "haxor",
        "hca": "hc alien",
        "hmd": "hemi-doppelganger",
        "hem": "hemophage",
        "hrb": "herbalist",
        "het": "heretic",
        "hob": "hobbit",
        "hom": "homicider",
        "hid": "hidden elf",
        "ill": "illithid",
        "imm": "immunizer",
        "imp": "imp",
        "ind": "indraenian",
        "ipl": "imperial",
        "ink": "inka",
        "ins": "insectoid",
        "iro": "ironman",
        "ita": "itaque",
        "jab": "jabberwock",
        "jap": "japura",
        "jav": "java",
        "jel": "jelly",
        "kha": "khajiit",
        "kop": "kop",
        "lep": "leprechaun",
        "lvs": "levelscaler",
        "lev": "levitator",
        "lic": "lich",
        "lis": "listener",
        "liz": "lizardman",
        "lol": "loli",
        "lyc": "lycanthrope",
        "mag": "magyar",
        "mai": "maia",
        "may": "maymes",
        "maz": "mazewalker",
        "met": "metal",
        "mim": "mimic",
        "min": "minimalist",
        "mis": "missingno",
        "mon": "monkey",
        "mnt": "monster",
        "moo": "moon elf",
        "mou": "mould",
        "mum": "mummy",
        "mus": "mushroom",
        "nag": "naga",
        "nas": "nastinator",
        "nav": "navi",
        "nor": "nord",
        "nul": "null",
        "nym": "nymph",
        "oct": "octopode",
        "pea": "peacemaker",
        "pha": "phantom",
        "pie": "piercer",
        "poi": "poisoner",
        "pol": "polyinitor",
        "pro": "problematic",
        "qua": "quantum mechanic",
        "rac": "race x",
        "ran": "randomizer",
        "red": "redditor",
        "rdg": "redguard",
        "rod": "rodneyan",
        "roh": "rohirrim",
        "rou": "rougelike",
        "sal": "salamander",
        "sat": "satre",
        "sea": "sea elf",
        "seg": "segfaulter",
        "sen": "senser",
        "she": "shell",
        "sho": "shoe",
        "sin": "sinner",
        "ske": "skeleton",
        "ski": "skillor",
        "sna": "snail",
        "sna": "snakeman",
        "sok": "sokosolver",
        "sov": "soviet",
        "spa": "spard",
        "spe": "specialist",
        "spd": "spiderman",
        "spi": "spirit",
        "spr": "spriggan",
        "sti": "sticker",
        "sus": "sustainer",
        "sux": "suxxor",
        "ter": "gerteut",
        "thu": "thunderlord",
        "tme": "turmene",
        "tri": "trainer",
        "trs": "transformer",
        "trp": "trapper",
        "tro": "troll",
        "tum": "tumblrer",
        "tur": "turtle",
        "umb": "umber hulk",
        "una": "unalignment thing",
        "und": "undefined",
        "ung": "ungenomold",
        "uni": "unicorn",
        "unm": "unmagic",
        "uri": "urian",
        "vmg": "vamgoyle",
        "vee": "veela",
        "ven": "venture capitalist",
        "vik": "viking",
        "vor": "vortex",
        "war": "warper",
        "win": "wind inhabitant",
        "wis": "wisp",
        "wor": "worm that walks",
        "wra": "wraith",
        "xor": "xorn",
        "yee": "yeek",
        "yok": "yokuda"
    }
    # save typing these out in multiple places
    vanilla_roles = ["arc","bar","cav","hea","kni","mon","pri",
                     "ran","rog","sam","tou","val","wiz"]
    vanilla_races = ["dwa","elf","gno","hum","orc"]

    # varname: ([aliases],[roles],[races])
    # first alias will be used for !variant
    # note this breaks if a player has the same name as an alias
    # so don't do that (I'm looking at you, FIQ)
    variants = {"nh343": (["nh343", "nethack", "343"],
                          vanilla_roles, vanilla_races),
                "nh363": (["nh363", "363", "363-hdf"],
                          vanilla_roles, vanilla_races),
                "nh370": (["nh370", "370", "370-hdf"],
                          vanilla_roles, vanilla_races),
                "nh13d": (["nh13d", "13d"],
                          vanilla_roles + ["elf", "fig", "nin"]),
                  "nh4": (["nethack4", "n4"],
                          vanilla_roles, vanilla_races),
                   "gh": (["grunthack", "grunt"],
                          vanilla_roles, vanilla_races + ["gia", "kob", "ogr"]),
                  "dnh": (["dnethack", "dn"],
                          vanilla_roles
                            + ["ana", "bin", "nob", "pir", "trb", "con"],
                          vanilla_races
                            + ["clk", "con", "bat", "dro", "hlf", "inc", "vam", "swn"]),
                 "ndnh": (["notdnethack", "ndn"],
                          vanilla_roles
                            + ["ana", "bin", "nob", "pir", "trb", "con", "acu"],
                          vanilla_races
                            + ["clk", "con", "bat", "dro", "hlf", "inc", "vam", "swn", "sal"]),
                   "un": (["unnethack", "unh"],
                          vanilla_roles + ["con"], vanilla_races),
                  "xnh": (["xnethack", "xnh"],
                          vanilla_roles, vanilla_races),
                  "spl": (["splicehack", "splice", "spl"],
                          vanilla_roles + ["car", "dgn", "dan"], vanilla_races + ["ang", "chg", "inf", "mer", "wlf"]),
                  "dyn": (["dynahack", "dyna"],
                          vanilla_roles + ["con"], vanilla_races + ["vam"]),
                   "fh": (["fiqhack"], # not "fiq" see comment above
                          vanilla_roles, vanilla_races),
                   "sp": (["sporkhack", "spork"],
                          vanilla_roles, vanilla_races),
                   "4k": (["nhfourk", "nhf", "fourk"],
                          vanilla_roles, vanilla_races + ["gia", "scu", "syl"]),
                "slshm": (["slash", "slash'em", "slshm"],
                          vanilla_roles + ["fla", "ice", "nec", "uds", "yeo"],
                          vanilla_races + ["dop", "dro", "hob", "lyc", "vam"]),
                 "slth": (["slashthem", "slth"],
                          vanilla_roles + ["fla", "ice", "nec", "uds", "yeo"],
                          vanilla_races + ["dop", "dro", "hob", "lyc", "vam"]),
                 "tnnt": (["tnnt"],
                          vanilla_roles, vanilla_races),
                 "seed": (["seed"],
                          vanilla_roles, vanilla_races),
                 "evil": (["evilhack", "evil", "evl"],
                          vanilla_roles + ["con"], vanilla_races + ["cen", "gia", "hob", "ill"]),
                 "slex": (["slex", "sloth", "amy's-weird-thing"],
                          vanilla_roles +
                             ["ana", "bin", "nob", "pir",
                              "trb", "con", "act", "alt",
                              "ama", "art", "ass", "aug",
                              "brd", "blo", "bos", "bul",
                              "cam", "cha", "che", "coo",
                              "cou", "cru", "dea", "div",
                              "dol", "doo", "dqs", "dri",
                              "dru", "dun", "elm", "ele",
                              "elp", "erd", "fai", "fea",
                              "fem", "fen", "fig", "fla",
                              "for", "fox", "gam", "gan",
                              "gns", "gee", "gla", "gof",
                              "gol", "gra", "gun", "ice",
                              "int", "jed", "jes", "jus",
                              "kor", "kur", "lad", "lib",
                              "loc", "lun", "mah", "med",
                              "mid", "mur", "mus", "nec",
                              "nin", "nuc", "occ", "off",
                              "ord", "pal", "pic", "poi",
                              "pok", "pol", "pro", "psi",
                              "rin", "roc", "sag", "sai",
                              "sci", "sha", "sla", "spa",
                              "sup", "tha", "top", "trc",
                              "trl", "trv", "twe", "unb",
                              "uds", "und", "use", "wan",
                              "war", "yeo", "yse", "zoo",
                              "zyb"],
                          vanilla_races +
                             ["gia", "kob", "ogr", "clk",
                              "inc", "vam", "yuk", "yok",
                              "scu", "syl", "add", "agg",
                              "ak ", "alb", "alc", "ali",
                              "ame", "amn", "anc", "anp",
                              "agb", "ang", "aqu", "arg",
                              "asg", "bas", "btm", "bor",
                              "bre", "bur", "cen", "chi",
                              "coc", "cor", "cur", "dar",
                              "dea", "dee", "des", "dev",
                              "dvl", "din", "dol", "dop",
                              "dra", "dry", "duf", "dun",
                              "ele", "ent", "evi", "exp",
                              "faw", "fel", "fen", "fie",
                              "gas", "gel", "gol", "gre",
                              "grm", "gri", "hax", "hmd",
                              "hem", "hrb", "het", "hob",
                              "hom", "hid", "ill", "imm",
                              "imp", "ipl", "ink", "ins",
                              "iro", "jel", "kha", "kop",
                              "lep", "lvs", "lev", "lic",
                              "lis", "liz", "lol", "lyc",
                              "mai", "maz", "mim", "min",
                              "mis", "mnt", "mon", "moo",
                              "mou", "mum", "nag", "nas",
                              "nav", "nor", "nul", "nym",
                              "oct", "pea", "pha", "poi",
                              "pol", "pro", "rac", "ran",
                              "red", "rdg", "rod", "roh",
                              "rou", "sal", "sat", "sea",
                              "seg", "sen", "sho", "sin",
                              "ske", "ski", "sna", "sna",
                              "sok", "sov", "spe", "spd",
                              "spi", "spr", "sti", "sus",
                              "sux", "thu", "tri", "trs",
                              "trp", "tro", "tum", "tur",
                              "una", "und", "ung", "uni",
                              "unm", "vmg", "vee", "ven",
                              "vor", "war", "win", "wis",
                              "wor", "wra", "xor", "yee"])}

    # variants which support streaks - now tracking slex streaks, because that's totally possible.
    streakvars = ["nh343", "nh363", "nh370", "nh13d", "gh", "dnh", "un", "sp", "xnh", "slex", "spl", "slshm", "tnnt", "ndnh", "evil", "seed", "slth"]
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
            print('sasl not available')
            self.quit('')
        sasl_string = '{0}\0{0}\0{1}'.format(self.nickname, self.password)
        sasl_b64_bytes = base64.b64encode(sasl_string.encode(encoding='UTF-8',errors='strict'))
        self.sendLine('AUTHENTICATE PLAIN')
        self.sendLine('AUTHENTICATE ' + sasl_b64_bytes.decode('UTF-8'))

    def irc_903(self, prefix, params):
        self.sendLine('CAP END')

    def irc_904(self, prefix, params):
        print('sasl auth failed', params)
        self.quit('')
    irc_905 = irc_904

    def signedOn(self):
        self.factory.resetDelay()
        self.startHeartbeat()
        if not SLAVE: self.join(CHANNEL)
        random.seed()

        self.logs = {}
        for xlogfile, (variant, delim, dumpfmt) in self.xlogfiles.items():
            self.logs[xlogfile] = (self.xlogfileReport, variant, delim, dumpfmt)
        for livelog, (variant, delim) in self.livelogs.items():
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
        try:
            self.tellbuf = shelve.open(BOTDIR + "/tellmsg.db", writeback=True)
        except:
            self.tellbuf = shelve.open(BOTDIR + "/tellmsg", writeback=True, protocol=2)

        # for !setmintc
        try:
            self.plr_tc = shelve.open(BOTDIR + "/plrtc.db", writeback=True)
        except:
            self.plr_tc = shelve.open(BOTDIR + "/plrtc", writeback=True, protocol=2)

        # Commands must be lowercase here.
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
                         "lastgame" : self.multiServerCmd,
                         "lastasc"  : self.multiServerCmd,
                         "scores"   : self.doScoreboard,
                         "sb"       : self.doScoreboard,
                         "rcedit"   : self.doRCedit,
                         "commands" : self.doCommands,
                         "help"     : self.doHelp,
                         "coltest"  : self.doColTest,
                         "players"  : self.multiServerCmd,
                         "who"      : self.multiServerCmd,
                         "asc"      : self.multiServerCmd,
                         "streak"   : self.multiServerCmd,
                         "whereis"  : self.multiServerCmd,
                         "8ball"    : self.do8ball,
                         "setmintc" : self.multiServerCmd,
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
                          "lastgame": self.getLastGame,
                          "setmintc": self.setPlrTC}
        # callbacks to run when all slaves have responded
        self.callBacks = {"players" : self.outPlayers,
                          "who"     : self.outPlayers,
                          "whereis" : self.outWhereIs,
                          "asc"     : self.outAscStreak,
                          "streak"  : self.outAscStreak,
                          # TODO: timestamp these so we can report the very last one
                          # For now, use the !asc/!streak callback as it's generic enough
                          "lastasc" : self.outAscStreak,
                          "lastgame": self.outAscStreak,
                          "setmintc": self.outPlrTC}

        # checkUsage outputs a message and returns false if input is bad
        # returns true if input is ok
        self.checkUsage ={"whereis" : self.usageWhereIs,
                          "asc"     : self.usageAsc,
                          "streak"  : self.usageStreak,
                          #"lastgame": self.usageLastGame,
                          #"lastasc" : self.usageLastAsc,
                          "setmintc": self.usagePlrTC}

        # seek to end of livelogs
        for filepath in self.livelogs:
            with filepath.open("r") as handle:
                handle.seek(0, 2)
                self.logs_seek[filepath] = handle.tell()

        # sequentially read xlogfiles from beginning to pre-populate lastgame data.
        for filepath in self.xlogfiles:
            with filepath.open("r") as handle:
                for line in handle.readlines():
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

    # Query/Response handling
    def doQuery(self, sender, replyto, msgwords):
        # called when slave gets queried by master.
        # msgwords is [ #Q#, <query_id>, <orig_sender>, <command>, ... ]
        if (sender in MASTERS) and (msgwords[3] in self.qCommands):
            # sender is passed to master; msgwords[2] is passed tp sender
            self.qCommands[msgwords[3]](sender,msgwords[2],msgwords[1],msgwords[3:])
        else:
            print("Bogus slave query from " + sender + ": " + " ".join(msgwords));

    def doResponse(self, sender, replyto, msgwords):
        # called when slave returns query response to master
        # msgwords is [ #R#, <query_id>, [server-tag], command output, ...]
        if sender in self.slaves and msgwords[1] in self.queries:
            self.queries[msgwords[1]]["resp"][sender] = " ".join(msgwords[2:])
            if set(self.queries[msgwords[1]]["resp"].keys()) >= set(self.slaves.keys()):
                #all slaves have responded
                self.queries[msgwords[1]]["callback"](self.queries.pop(msgwords[1]))
        else:
            print("Bogus slave response from " + sender + ": " + " ".join(msgwords));

    def timeoutQuery(self, query):
        if query not in self.queries: return # query was completed before timeout
        # probably should handle the 'no slaves responded' case better than this.
        self.queries[query]["callback"](self.queries.pop(query))

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

    def doRip(self, sender, replyto, msgwords = 0):
        self.msg(replyto, "rip")

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
            try:
                self.respond(replyto, sender, str(random.randrange(int(rngrange[0]), int(rngrange[-1])+1)))
            except ValueError:
                try: # maybe some smart arse reversed the values
                    self.respond(replyto, sender, str(random.randrange(int(rngrange[-1]), int(rngrange[0])+1)))
                except ValueError:
                    # Nonsense input. Recurse with no args for usage message.
                    self.doRng(sender, replyto, [msgwords[0]])
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
           v = random.choice(list(self.variants.keys()))
           self.respond(replyto, sender, self.variants[v][0][0] + " " + self.rolename[random.choice(self.variants[v][1])])

    def doRace(self, sender, replyto, msgwords):
        if len(msgwords) > 1:
           v = self.varalias(msgwords[1])
           #error if variant not found
           if not self.variants.get(v,False):
               self.respond(replyto, sender, "No variant " + msgwords[1] + " on server.")
           self.respond(replyto, sender, self.racename[random.choice(self.variants[v][2])])
        else:
           v = random.choice(list(self.variants.keys()))
           self.respond(replyto, sender, self.variants[v][0][0] + " " + self.racename[random.choice(self.variants[v][2])])

    def doVariant(self, sender, replyto, msgwords):
        self.respond(replyto, sender, self.variants[random.choice(list(self.variants.keys()))][0][0])

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
                       "tea"   : ["cup", "mug", "saucer"],
                       "potion": ["potion", "vial", "droplet"],
                       "booze" : ["shot", "tall glass", "tumbler", "glass", "schooner", "pint", "fifth", "barrel", "flask"],
                       "coffee": ["cup", "mug"],
                       "vodka" : ["shot", "tall glass", "tumbler", "glass"],
                       "whiskey":["shot", "tall glass", "tumbler", "glass", "flask"],
                       "rum"   : ["shot", "tall glass", "tumbler", "glass"],
                       "tequila":["shot", "tall glass", "tumbler", "glass"],
                       "scotch": ["shot", "tall glass", "tumbler", "glass", "flask"]
                       # others omitted - anything goes for them
                      },

            "drink" : {"tea"   : ["black", "white", "green", "polka-dot", "Earl Grey", "oolong", "darjeeling"],
                       "potion": ["water", "fruit juice", "see invisible", "sickness", "confusion", "extra healing", "hallucination", "healing", "holy water", "unholy water", "restore ability", "sleeping", "blindness", "gain energy", "invisibility", "monster detection", "object detection", "booze", "enlightenment", "full healing", "levitation", "polymorph", "speed", "acid", "oil", "gain ability", "gain level", "paralysis"],
                       "booze" : ["booze", "the hooch", "moonshine", "the sauce", "grog", "suds", "the hard stuff", "liquid courage", "grappa"],
                       "coffee": ["coffee", "espresso", "cafe latte", "Blend 43"],
                       "vodka" : ["Stolichnaya", "Absolut", "Grey Goose", "Ketel One", "Belvedere", "Luksusowa", "SKYY", "Finlandia", "Smirnoff"],
                       "whiskey":["Irish", "Jack Daniels", "Evan Williams", "Crown Royal", "Crown Royal Reserve", "Johnnie Walker Black", "Johnnie Walker Red", "Johnnie Walker Blue"],
                       "rum"   : ["Bundy", "Jamaican", "white", "dark", "spiced", "pirate"],
                       "fictional": ["Romulan ale", "Blood wine", "Kanar", "Pan Galactic Gargle Blaster", "jynnan tonyx", "gee-N'N-T'N-ix", "jinond-o-nicks", "chinanto/mnigs", "tzjin-anthony-ks", "Moloko Plus", "Duff beer", "Panther Pilsner beer", "Screaming Viking", "Blue milk", "Fizzy Bubblech", "Butterbeer", "Ent-draught", "Nectar of the Gods", "Frobscottle"],
                       "tequila":["blanco", "oro", "reposado", "añejo", "extra añejo", "Patron Silver", "Jose Cuervo 1800"],
                       "scotch": ["single malt", "single grain", "blended malt", "blended grain", "blended", "Glenfiddich", "Glenlivet", "Dalwhinnie"],
                       "junk"  : ["blended kale", "pickle juice", "poorly-distilled rocket fuel", "caustic gas", "liquid smoke", "protein shake", "wheatgrass nonsense", "olive oil", "saline solution", "napalm", "synovial fluid", "drool"]},
            "prepared":["brewed", "distilled", "fermented", "decanted", "prayed over", "replicated", "conjured", "acquired", "brewed", "excreted"],
            "degrees" :{"Kelvin": [0, 500], "degrees Celsius": [-20,95], "degrees Fahrenheit": [-20,200]}, #sane-ish ranges
            "suppress": ["coffee", "junk", "booze", "potion", "fictional"] } # do not append these to the random description


    def doTea(self, sender, replyto, msgwords):
        if len(msgwords) > 1: target = msgwords[1]
        else: target = sender
        drink = random.choice([msgwords[0]] * 50 + list(self.bev["drink"].keys()))
        for vchoice in range(10):
            vessel = random.choice(self.bev["vessel"]["all"])
            if drink not in self.bev["vessel"].keys(): break # anything goes for these
            if vessel in self.bev["vessel"][drink]: break # match!
        fulldrink = random.choice(self.bev["drink"][drink])
        if drink not in self.bev["suppress"]: fulldrink += " " + drink
        tempunit = random.choice(list(self.bev["degrees"].keys()))
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
        if len(msgwords) < 3:
            self.respond(replyto, sender, "!tell <recipient> <message> (leave a message for someone)")
            return
        willDo = [ "Will do, {0}!",
                   "I'm on it, {0}.",
                   "No worries, {0}, I've got this!",
                   "{1} shall be duly informed at the first opportunity, {0}." ]

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
        self.msgLog(replyto,random.choice(willDo).format(sender,rcpt))

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
            print("forwardQuery: " + sl)
            self.msg(sl,message)
        # set up the timeout in 5 seconds.
        reactor.callLater(5, self.timeoutQuery, q)

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
                    plrvar += inpfile.split("/")[-1].split(":")[0] + " " + self.displaytag(var) + " "
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
                                    wirec = parse_xlogfile_line(open(wipath, "rb").read(),":")

                                    self.msg(master, "#R# " + query
                                             + " " + self.displaytag(SERVERTAG) + " " + plr
                                             + " "+self.displaytag(var)
                                             + ": ({role} {race} {gender} {align}) T:{turns} ".format(**wirec)
                                             + self.dungeons[var][wirec["dnum"]]
                                             + " level: " + str(wirec["depth"])
                                             + ammy[wirec["amulet"]])
                                    return

                        self.msg(master, "#R# " + query + " "
                                                + self.displaytag(SERVERTAG)
                                                + " " + plr + " "
                                                + self.displaytag(var)
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


    def plrVar(self, sender, replyto, msgwords):
        # for !streak and !asc, work out what player and variant they want
        if len(msgwords) > 3:
            # !streak tom dick harry
            if not SLAVE: self.respond(replyto,sender,"Usage: !" +msgwords[0] +" [variant] [player]")
            return(None, None)
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
            if not SLAVE: self.respond(replyto,sender,"Usage: !" +msgwords[0] +" [variant] [player]")
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

    def usageAsc(self, sender, replyto, msgwords):
        if self.plrVar(sender, replyto, msgwords)[0]:
            return True
        return False

    def getAsc(self, master, sender, query, msgwords):
        (PLR, var) = self.plrVar(sender, "", msgwords)
        if not PLR: return # bogus input, should have been handled in usage check above
        plr = PLR.lower()
        stats = ""
        totasc = 0
        if var:
            if not plr in self.asc[var]:
                repl = self.displaytag(SERVERTAG) + " No ascensions for " + PLR + " in "
                if plr in self.allgames[var]:
                    repl += str(self.allgames[var][plr]) + " games of "
                repl += self.variants[var][0][0] + "."
                self.msg(master,"#R# " + query + " " + repl)
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
            self.msg(master, "#R# " + query + " " + self.displaytag(SERVERTAG)
                             + " " + PLR
                             + " has ascended " + self.variants[var][0][0] + " "
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
            self.msg(master, "#R# " + query + " "
                         + self.displaytag(SERVERTAG) + " " + PLR
                         + " has ascended " + str(totasc) + " times in "
                         + str(totgames)
                         + " games ({:0.2f}%): ".format((100.0 * totasc) / totgames)
                         + stats)
            return
        if totgames:
            self.msg(master, "#R# " + query + " " + self.displaytag(SERVERTAG) + " " + PLR
                                    + " has not ascended in " + str(totgames) + " games.")
            return
        self.msg(master, "#R# " + query + " No games for " + PLR + ".")
        return

    def outAscStreak(self,q):
        msgs = []
        fallback_msg = ""
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
        (p,v) = self.plrVar(sender, replyto, msgwords)
        if not p: return False
        if v:
            if v not in self.streakvars:
                self.respond(replyto,sender,"Streaks are not recorded for " + v +".")
                return False
        return True

    def streakDate(self,stamp):
        return datetime.datetime.fromtimestamp(float(stamp)).strftime("%Y-%m-%d")
        #return stamp.strftime("%Y-%m-%d")

    def getStreak(self, master, sender, query, msgwords):
        (PLR, var) = self.plrVar(sender, "", msgwords)
        if not PLR: return # bogus input, handled by usage check.
        plr = PLR.lower()
        reply = "#R# " + query + " "
        if var:
            (lstart,lend,llength) = self.longstreak[var].get(plr,(0,0,0))
            (cstart,cend,clength) = self.curstreak[var].get(plr,(0,0,0))
            if llength == 0:
                reply += "No streaks for " + PLR + self.displaytag(var) + "."
                self.msg(master,reply)
                return
            reply += self.displaytag(SERVERTAG) + " " + PLR + self.displaytag(var)
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
        (lmax,cmax) = (0,0)
        for var in self.streakvars:
            (lstart,lend,llength) = self.longstreak[var].get(plr,(0,0,0))
            (cstart,cend,clength) = self.curstreak[var].get(plr,(0,0,0))
            if llength > lmax:
                (lmax, lvar, lsmax, lemax)  = (llength, var, lstart, lend)
            if clength > cmax:
                (cmax, cvar, csmax, cemax)  = (clength, var, cstart, cend)
        if lmax == 0:
            reply += "No streaks for " + PLR + "."
            self.msg(master, reply)
            return
        reply += self.displaytag(SERVERTAG) + " " + PLR + " Max[" + self.displaystring[lvar] + "]: " + str(lmax)
        reply += " (" + self.streakDate(lsmax) \
                      + " - " + self.streakDate(lemax) + ")"
        if cmax > 0:
            if csmax == lsmax:
                reply += "(current)"
            else:
                reply += ". Current[" + self.displaystring[cvar] + "]: " + str(cmax)
                reply += " (since " + self.streakDate(csmax) + ")"
        reply += "."
        self.msg(master, reply)

    def getLastGame(self, master, sender, query, msgwords):
        if (len(msgwords) >= 3): #var, plr, any order.
            vp = self.varalias(msgwords[1])
            pv = self.varalias(msgwords[2])
            dl = self.lg.get(":".join([vp,pv]).lower(), False)
            if not dl:
                dl = self.lg.get(":".join([pv,vp]).lower(),False)
            if not dl:
                self.msg(master, "#R# " + query +
                                 " No last game for (" + ",".join(msgwords[1:3]) + ").")
                return
            # TODO: Add timestamp to message so we can just output most recent across servers
            self.msg(master, "#R# " + query + " " + self.displaytag(SERVERTAG) + " " + dl)
            return
        if (len(msgwords) == 2): #var OR plr - don't care which
            vp = self.varalias(msgwords[1])
            dl = self.lg.get(vp,False)
            if not dl:
                self.msg(master, "#R# " + query +
                                 " No last game for " + msgwords[1] + ".")
                return
            self.msg(master, "#R# " + query + " " + self.displaytag(SERVERTAG) + " " + dl)
            return
        self.msg(master, "#R# " + query + " " + self.displaytag(SERVERTAG) + " " + self.lastgame)

    def getLastAsc(self, master, sender, query, msgwords):
        if (len(msgwords) >= 3): #var, plr, any order.
            vp = self.varalias(msgwords[1])
            pv = self.varalias(msgwords[2])
            dl = self.la.get(":".join([pv,vp]).lower(),False)
            if not dl:
                dl = self.la.get(":".join([vp,pv]).lower(),False)
            if not dl:
                self.msg(master, "#R# " + query +
                                 " No last ascension for (" + ",".join(msgwords[1:3]) + ").")
                return
            # TODO: Add timestamp to message so we can just output most recent across servers
            self.msg(master, "#R# " + query + " " + self.displaytag(SERVERTAG) + " " + dl)
            return
        if (len(msgwords) == 2): #var OR plr - don't care which
            vp = self.varalias(msgwords[1])
            dl = self.la.get(vp,False)
            if not dl:
                self.msg(master, "#R# " + query +
                                 " No last ascension for " + msgwords[1] + ".")
                return
            self.msg(master, "#R# " + query + " " + self.displaytag(SERVERTAG) + " " + dl)
            return
        self.msg(master, "#R# " + query + " " + self.displaytag(SERVERTAG) + " " + self.lastasc)

    # Allows players to set minimum turncount of their games to be reported
    # so they can manage their own deathspam
    # turncount may not be the best metric for this - open to suggestions
    # player name must match nick, or can be set by an admin.

    def usagePlrTC(self, sender, replyto, msgwords):
        if len(msgwords) > 2 and sender not in self.admin:
            self.respond(replyto, sender, "Usage: !" + msgwords[0] + " [turncount]")
            return False
        return True

    def setPlrTC(self, master, sender, query, msgwords):
        if len(msgwords) == 2:
            if re.match(r'^\d+$',msgwords[1]):
                self.plr_tc[sender.lower()] = int(msgwords[1])
                self.plr_tc.sync()
                self.msg(master, "#R# " + query + " " + self.displaytag(SERVERTAG)
                                 + " Min reported turncount for " + sender.lower()
                                 + " set to " + msgwords[1])
                return
        if len(msgwords) == 1:
            if sender.lower() in self.plr_tc.keys():
                del self.plr_tc[sender.lower()]
                self.plr_tc.sync()
                self.msg(master, "#R# " + query + " " + self.displaytag(SERVERTAG)
                                 + " Min reported turncount for " + sender.lower()
                                 + " removed.")
            else:
                self.msg(master, "#R# " + query + " No min turncount for " + sender.lower())
            return
        if sender in self.admin:
            if len(msgwords) == 3:
                if re.match(r'^\d+$',msgwords[2]):
                    self.plr_tc[msgwords[1].lower()] = int(msgwords[2])
                    self.plr_tc.sync()
                    self.msg(master, "#R# " + query + " " + self.displaytag(SERVERTAG)
                                     + " Min reported turncount for " + msgwords[1].lower()
                                     + " set to " + msgwords[2])
                    return
            if len(msgwords) == 2:
                if msgwords[1].lower() in self.plr_tc.keys():
                    del self.plr_tc[msgwords[1].lower()]
                    self.plr_tc.sync()
                    self.msg(master, "#R# " + query + " " + self.displaytag(SERVERTAG)
                                     + " Min reported turncount for " + msgwords[1].lower()
                                     + " removed.")
                else:
                    self.msg(master, "#R# " + query + " No min turncount for " + msgwords[1].lower())
                return

    def outPlrTC(self,q):
        outmsg = ''
        for server in q["resp"]:
            firstword = q["resp"][server].split(' ')[0]
            if firstword == 'No':
                fallback_msg = q["resp"][server]
            elif not outmsg:
                outmsg = q["resp"][server]
            else: # just prepend server tags to message
                outmsg = firstword + outmsg
        if not outmsg: outmsg = fallback_msg
        self.respond(q["replyto"],q["sender"],outmsg)

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
        if re.match(r'^(rip|r\.i\.p|rest in p).*$', message.lower()):
            self.doRip(sender, replyto)
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
            self.msgLog(CHANNEL, " ".join(msgwords))

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

    # players can request their deaths and other events not be reported if less than x turns
    def plr_tc_notreached(self, name, turns):
        return (name.lower() in self.plr_tc.keys()
           and turns < self.plr_tc[name.lower()])

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
            dumpurl = urllib.parse.quote(game["dumpfmt"].format(**game))
            dumpurl = self.dump_url_prefix.format(**game) + dumpurl
        self.lg["{variant}:{name}".format(**game).lower()] = dumpurl
        if (game["endtime"] > self.lge.get(lname, 0)):
            self.lge[lname] = game["endtime"]
            self.lg[lname] = dumpurl
        self.lg[var] = dumpurl
        if (game["endtime"] > self.tlastgame):
            self.lastgame = dumpurl
            self.tlastgame = game["endtime"]

        # Kludge for nethack 1.3d -
        # populate race and align with dummy values.
        if "race" not in game: game["race"] = "###"
        if "align" not in game: game["align"] = "###"

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
            if self.plr_tc_notreached(game["name"], game["turns"]): return # ignore due to !setmintc, only if not ascended
        # end of statistics gathering

        if (not report): return # we're just reading through old entries at startup

        # format duration string based on realtime and/or wallclock duration
        if "starttime" in game and "endtime" in game:
            game["wallclock"] = timedelta_int(game["endtime"] - game["starttime"])
        if "realtime" in game and "wallclock" in game:
            if game["realtime"] == game["wallclock"]:
                game["duration_str"] = f"[{game['realtime']}]"
            else:
                game["duration_str"] = f"rt[{game['realtime']}], wc[{game['wallclock']}]"
        elif "realtime" in game and "wallclock" not in game:
                game["duration_str"] = f"rt[{game['realtime']}]"
        elif "wallclock" in game and "realtime" not in game:
                game["duration_str"] = f"wc[{game['realtime']}]"

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
            if game.get("version","unknown") == "NH-1.3d":
                yield ("[{displaystring}] {name} ({role} {gender}), "
                       "{points} points, T:{turns}, {death}{ascsuff}").format(**game)
            elif var == "seed" and "duration_str" in game:
                yield ("[{displaystring}] {name} ({role} {race} {gender} {align}), "
                       "{points} points, T:{turns}, {duration_str}, {death}{ascsuff}").format(**game)
            else:
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
                    if self.plr_tc_notreached(event["player"], event["turns"]): return
                    event["player"] = "{charname} ({player})".format(**event)
            else:
                event["player"] = event["charname"]

        if self.plr_tc_notreached(event["player"], event["turns"]): return

        # 1.3d kludge again
        if "race" not in event: event["race"] = "###"
        if "align" not in event: event["align"] = "###"

        if "historic_event" in event and "message" not in event:
            if event["historic_event"].endswith("."):
                event["historic_event"] = event["historic_event"][:-1]
            event["message"] = event["historic_event"]
        if "lltype" in event:
            for t in  LL_TURNCOUNTS:
                if event["turns"] < LL_TURNCOUNTS[t]:
                    event["lltype"] &= ~t
                    if not event["lltype"]: return
        if "message" in event:
            if event["message"] == "entered the Dungeons of Doom":
                if "user_seed" in event and "seed" in event and event["user_seed"]:
                    yield("[{displaystring}] {player} ({role} {race} {gender} {align}) "
                    "{message} [chosen seed: {seed}]".format(**event))
                else:
                    yield("[{displaystring}] {player} ({role} {race} {gender} {align}) "
                    "{message} [random seed]".format(**event))
            elif "realtime" in event:
                event["realtime_fmt"] = str(event["realtime"])
                yield ("[{displaystring}] {player} ({role} {race} {gender} {align}) "
                       "{message}, on T:{turns}, rt[{realtime_fmt}]").format(**event)
            else:
                yield ("[{displaystring}] {player} ({role} {race} {gender} {align}) "
                       "{message}, on T:{turns}").format(**event)
        elif "wish" in event:
            yield ("[{displaystring}] {player} ({role} {race} {gender} {align}) "
                   'wished for "{wish}", on T:{turns}').format(**event)
        elif "shout" in event:
            yield ("[{displaystring}] {player} ({role} {race} {gender} {align}) "
                   'shouted "{shout}", on T:{turns}').format(**event)
        elif "bones_killed" in event:
            if not event.get("bones_rank",False): # fourk does not have bones rank so use role instead
                event["bones_rank"] = event["bones_role"]
            yield ("[{displaystring}] {player} ({role} {race} {gender} {align}) "
                   "killed the {bones_monst} of {bones_killed}, "
                   "the former {bones_rank}, on T:{turns}").format(**event)
        elif "killed_uniq" in event:
            yield ("[{displaystring}] {player} ({role} {race} {gender} {align}) "
                   "killed {killed_uniq}, on T:{turns}").format(**event)
        elif "defeated" in event: # fourk uses this instead of killed_uniq.
            yield ("[{displaystring}] {player} ({role} {race} {gender} {align}) "
                   "defeated {defeated}, on T:{turns}").format(**event)
        # more 1.3d shite
        elif "genocided_monster" in event:
            if event.get("dungeon_wide","yes") == "yes":
                event["genoscope"] = "dungeon wide";
            else:
                event["genoscope"] = "locally";
            yield ("[{displaystring}] {player} ({role} {race} {gender} {align}) "
                   "genocided {genocided_monster} {genoscope} on T:{turns}").format(**event)
        elif "shoplifted" in event:
            yield ("[{displaystring}] {player} ({role} {race} {gender} {align}) "
                   "stole {shoplifted} zorkmids of merchandise from the {shop} of"
                   " {shopkeeper} on T:{turns}").format(**event)
        elif "killed_shopkeeper" in event:
            yield ("[{displaystring}] {player} ({role} {race} {gender} {align}) "
                   "killed {killed_shopkeeper} on T:{turns}").format(**event)

    def connectionLost(self, reason=None):
        if self.looping_calls is None: return
        for call in self.looping_calls.values():
            call.stop()

    def logReport(self, filepath):
        with filepath.open("r") as handle:
            handle.seek(self.logs_seek[filepath])

            for line in handle.readlines():
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

class DeathBotFactory(ReconnectingClientFactory):
    def startedConnecting(self, connector):
        print('Started to connect.')

    def buildProtocol(self, addr):
        print('Connected.')
        print('Resetting reconnection delay')
        self.resetDelay()
        p = DeathBotProtocol()
        p.factory = self
        return p

    def clientConnectionLost(self, connector, reason):
        print('Lost connection.  Reason:', reason)
        ReconnectingClientFactory.clientConnectionLost(self, connector, reason)

    def clientConnectionFailed(self, connector, reason):
        print('Connection failed. Reason:', reason)
        ReconnectingClientFactory.clientConnectionFailed(self, connector,
                                                         reason)

#if __name__ == "__main__":
#    f = protocol.ReconnectingClientFactory()
#    f.protocol = DeathBotProtocol()
#    application = service.Application("DeathBot")
#    deathservice = internet.SSLClient(HOST, PORT, f,
#                                      ssl.ClientContextFactory())
#    deathservice.setServiceParent(application)


if __name__ == '__main__':
    # initialize logging
    log.startLogging(DailyLogFile.fromFullPath(LOGBASE))

    # create factory protocol and application
    f = DeathBotFactory()

    # connect factory to this host and port
    reactor.connectSSL(HOST, PORT, f, ssl.ClientContextFactory())

    # run bot
    reactor.run()
