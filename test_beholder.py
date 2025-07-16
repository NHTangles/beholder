#!/usr/bin/env python3
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
import os       # for check path exists (dumplogs), and chmod
import stat     # for chmod mode bits
import re       # for hello, and other things.
import urllib   # for dealing with NH4 variants' #&$#@ spaces in filenames.
import shelve   # for persistent !tell messages
import random   # for !rng and friends
import glob     # for matching in !whereis
import requests # for !rumor

# Configuration constants for timeouts and limits
QUERY_TIMEOUT = 5  # Timeout for queries in seconds
MAX_VARIANT_CHOICES = 10  # Maximum random variant choices
LOG_CHECK_INTERVAL = 3  # How often to check log files (seconds)
FILE_MONITOR_INTERVAL = 1  # How often to check for file changes (seconds)
MAX_QUERIES = 100  # Maximum concurrent queries to prevent memory leaks
MAX_TELLBUF_MESSAGES = 1000  # Maximum total tell messages stored
RATE_LIMIT_WINDOW = 60  # Rate limiting time window in seconds
RATE_LIMIT_COMMANDS = 60   # Commands per minute for all operations (1/second)
BURST_WINDOW = 1        # Burst protection: only 1 command per second window
ABUSE_THRESHOLD = 10    # Consecutive commands before abuse penalty
ABUSE_WINDOW = 30       # Time window for abuse detection (seconds)
ABUSE_PENALTY = 900     # Abuse penalty duration in seconds (15 minutes)
RESPONSE_RATE_LIMIT = 1   # Max penalty messages per 2 minutes to prevent spam
RESPONSE_RATE_WINDOW = 120  # Penalty message rate limit window (2 minutes)

# Pre-compiled regex patterns for better performance
RE_COLOR_FG_BG = re.compile(r'\x03\d\d,\d\d')  # fg,bg pair
RE_COLOR_FG = re.compile(r'\x03\d\d')  # fg only
RE_COLOR_END = re.compile(r'[\x1D\x03\x0f]')  # end of colour and italics
RE_DICE_CMD = re.compile(r'^\d*d$')  # !d, !4d is rubbish input
RE_DIGITS = re.compile(r'^\d+$')  # match only digits
RE_DICE_FULL = re.compile(r'^\d*d\d*$')  # full dice pattern
RE_HELLO = re.compile(r'^(hello|hi|hey|salut|hallo|guten tag|shalom|ciao|hola|aloha|bonjour|hei|gday|konnichiwa|nuqneh)[!?. ]*$', re.IGNORECASE)
RE_SPACE_COLOR = re.compile(r'^ [\x1D\x03\x0f]*')  # space and color codes

site.addsitedir('.')
from test_botconf import HOST, PORT, CHANNEL, NICK, USERNAME, REALNAME, BOTDIR
from test_botconf import PWFILE, FILEROOT, WEBROOT, LOGROOT, PINOBOT, ADMIN
from test_botconf import SERVERTAG

#try: from botconf import LOGBASE
#except: LOGBASE = "/var/log/Beholder.log"
try: from test_botconf import LL_TURNCOUNTS
except: LL_TURNCOUNTS = {}
try: from test_botconf import DCBRIDGE
except: DCBRIDGE = None
try: from test_botconf import TEST
except: TEST = False
try:
    from test_botconf import REMOTES
except:
    SLAVE = True #if we have no slaves, we (probably) are the slave
    REMOTES = {}
try:
    from test_botconf import MASTERS
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

def safe_int_parse(s):
    """Safely parse integers, including hex values like 0x1234"""
    try:
        # Try to parse as int, supports base 10, hex (0x), octal (0o), binary (0b)
        return int(s, 0)
    except ValueError:
        # If that fails, try without base detection
        try:
            return int(s)
        except ValueError:
            return 0  # Default to 0 for invalid values

xlogfile_parse = dict.fromkeys(
    ("points", "deathdnum", "deathlev", "maxlvl", "hp", "maxhp", "deaths",
     "starttime", "curtime", "endtime", "user_seed",
     "uid", "turns", "xplevel", "exp","depth","dnum","score","amulet", "lltype"), int)
xlogfile_parse.update(dict.fromkeys(
    ("conduct", "event", "carried", "flags", "achieve"), safe_int_parse))
xlogfile_parse["realtime"] = timedelta_int

def sanitize_format_string(text):
    """Sanitize text to prevent format string injection attacks.

    Escapes curly braces that could be used in format string attacks.
    """
    if not isinstance(text, str):
        return text
    return text.replace('{', '{{').replace('}', '}}')

def parse_xlogfile_line(line, delim):
    record = {}
    # Fields that contain user-controlled text that could be used in format strings
    user_controlled_fields = {'name', 'charname', 'death', 'killer', 'wish',
                              'shout', 'genocided_monster', 'bones_killed',
                              'bones_monst', 'killed_uniq', 'defeated',
                              'shopkeeper', 'killed_shopkeeper'}

    for field in line.strip().decode(encoding='UTF-8', errors='ignore').split(delim):
        key, _, value = field.partition("=")
        if key in xlogfile_parse:
            value = xlogfile_parse[key](value)
        # Sanitize user-controlled fields to prevent format string injection
        elif key in user_controlled_fields:
            value = sanitize_format_string(value)
        record[key] = value
    return record

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
        with open(PWFILE, "r") as f:
            password = f.read().strip()
    except (IOError, FileNotFoundError) as e:
        print(f"Warning: Could not read password file {PWFILE}: {e}")
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
        os.chmod(chanLogName,stat.S_IRUSR|stat.S_IWUSR)

    xlogfiles = {filepath.FilePath(FILEROOT+"nh343-hdf/var/xlogfile"): ("nh343", ":", "nh343/dumplog/{starttime}.nh343.txt"),
                 filepath.FilePath(FILEROOT+"nh363-hdf/var/xlogfile"): ("nh363", "\t", "nethack/dumplog/{starttime}.nh.html"),
                 filepath.FilePath(FILEROOT+"nh370.127-hdf/var/xlogfile"): ("nh370", "\t", "nethack/dumplog/{starttime}.nh.html"),
                 filepath.FilePath(FILEROOT+"grunthack-0.3.0/var/xlogfile"): ("gh", ":", "gh/dumplog/{starttime}.gh.txt"),
                 filepath.FilePath(FILEROOT+"dnethack-3.24.0/xlogfile"): ("dnh", ":", "dnethack/dumplog/{starttime}.dnh.txt"),
                 filepath.FilePath(FILEROOT+"fiqhackdir/data/xlogfile"): ("fh", ":", "fiqhack/dumplog/{dumplog}"),
                 filepath.FilePath(FILEROOT+"dynahack/dynahack-data/var/xlogfile"): ("dyn", ":", "dynahack/dumplog/{dumplog}"),
                 filepath.FilePath(FILEROOT+"nh4dir/save/xlogfile"): ("nh4", ":", "nethack4/dumplog/{dumplog}"),
                 filepath.FilePath(FILEROOT+"fourkdir-4.3.0.5/save/xlogfile"): ("4k", "\t", "nhfourk/dumps/{dumplog}"),
                 filepath.FilePath(FILEROOT+"sporkhack-0.7.0/var/xlogfile"): ("sp", "\t", "sporkhack/dumplog/{starttime}.sp.txt"),
                 filepath.FilePath(FILEROOT+"xnethack-9.0.0/var/xlogfile"): ("xnh", "\t", "xnethack/dumplog/{starttime}.xnh.html"),
                 filepath.FilePath(FILEROOT+"splicehack-1.2.0/var/xlogfile"): ("spl", "\t", "splicehack/dumplog/{starttime}.splice.html"),
                 filepath.FilePath(FILEROOT+"nh13d/xlogfile"): ("nh13d", ":", "nh13d/dumplog/{starttime}.nh13d.txt"),
                 filepath.FilePath(FILEROOT+"slashem-0.0.8E0F2/xlogfile"): ("slshm", ":", "slashem/dumplog/{starttime}.slashem.txt"),
                 filepath.FilePath(FILEROOT+"notdnethack-2025.05.15/xlogfile"): ("ndnh", ":", "notdnethack/dumplog/{starttime}.ndnh.txt"),
                 filepath.FilePath(FILEROOT+"notnotdnethack-2025.05.16/xlogfile"): ("nndnh", ":", "notnotdnethack/dumplog/{starttime}.nndnh.txt"),
                 filepath.FilePath(FILEROOT+"evilhack-0.9.1/var/xlogfile"): ("evil", "\t", "evilhack/dumplog/{starttime}.evil.html"),
                 filepath.FilePath(FILEROOT+"slashthem-0.9.7/xlogfile"): ("slth", ":", "slashthem/dumplog/{starttime}.slth.txt"),
                 filepath.FilePath(FILEROOT+"gnollhack-4.2.0.41/var/xlogfile"): ("gnoll", "\t", "gnollhack/dumplog/{starttime}.gnoll.html"),
                 filepath.FilePath(FILEROOT+"acehack/xlogfile"): ("ace", ":", "acehack/dumplog/{starttime}.ace.txt"),
                 filepath.FilePath(FILEROOT+"hackem-1.3.2/var/xlogfile"): ("hackm", "\t", "hackem/dumplog/{starttime}.hackem.html"),
                 filepath.FilePath(FILEROOT+"nethackathon/var/xlogfile"): ("nhthon", "\t", "nethackathon/dumplog/{starttime}.nhthon.html"),
                 filepath.FilePath(FILEROOT+"tnnt/var/xlogfile"): ("tnnt", "\t", "tnnt/dumplog/{starttime}.tnnt.html"),
                 filepath.FilePath(FILEROOT+"nerfhack-2.2.1/var/xlogfile"): ("nerf", "\t", "nerfhack/dumplog/{starttime}.nerf.html"),
                 filepath.FilePath(FILEROOT+"crecellehack-1.0.1/var/xlogfile"): ("cre", "\t", "crecellehack/dumplog/{starttime}.cre.html"),
                 filepath.FilePath(FILEROOT+"unnethack-6.0.14/var/xlogfile"): ("un", "\t", "unnethack/dumplog/{starttime}.un.txt.html")}
    livelogs  = {filepath.FilePath(FILEROOT+"nh343-hdf/var/livelog"): ("nh343", ":"),
                 filepath.FilePath(FILEROOT+"nh363-hdf/var/livelog"): ("nh363", "\t"),
                 filepath.FilePath(FILEROOT+"nh370.127-hdf/var/livelog"): ("nh370", "\t"),
                 filepath.FilePath(FILEROOT+"grunthack-0.3.0/var/livelog"): ("gh", ":"),
                 filepath.FilePath(FILEROOT+"dnethack-3.24.0/livelog"): ("dnh", ":"),
                 filepath.FilePath(FILEROOT+"fourkdir-4.3.0.5/save/livelog"): ("4k", "\t"),
                 filepath.FilePath(FILEROOT+"fiqhackdir/data/livelog"): ("fh", ":"),
                 filepath.FilePath(FILEROOT+"sporkhack-0.7.0/var/livelog"): ("sp", ":"),
                 filepath.FilePath(FILEROOT+"xnethack-9.0.0/var/livelog"): ("xnh", "\t"),
                 filepath.FilePath(FILEROOT+"splicehack-1.2.0/var/livelog"): ("spl", "\t"),
                 filepath.FilePath(FILEROOT+"nh13d/livelog"): ("nh13d", ":"),
                 filepath.FilePath(FILEROOT+"slashem-0.0.8E0F2/livelog"): ("slshm", ":"),
                 filepath.FilePath(FILEROOT+"notdnethack-2025.05.15/livelog"): ("ndnh", ":"),
                 filepath.FilePath(FILEROOT+"notnotdnethack-2025.05.16/livelog"): ("nndnh", ":"),
                 filepath.FilePath(FILEROOT+"evilhack-0.9.1/var/livelog"): ("evil", "\t"),
                 filepath.FilePath(FILEROOT+"slashthem-0.9.7/livelog"): ("slth", ":"),
                 filepath.FilePath(FILEROOT+"gnollhack-4.2.0.41/var/livelog"): ("gnoll", "\t"),
                 filepath.FilePath(FILEROOT+"acehack/livelog"): ("ace", ":"),
                 filepath.FilePath(FILEROOT+"hackem-1.3.2/var/livelog"): ("hackm", "\t"),
                 filepath.FilePath(FILEROOT+"nerfhack-2.2.1/var/livelog"): ("nerf", "\t"),
                 filepath.FilePath(FILEROOT+"crecellehack-1.0.1/var/livelog"): ("cre", "\t"),
                 filepath.FilePath(FILEROOT+"unnethack-6.0.14/var/livelog"): ("un", "\t")}

    # Forward events to other bots at the request of maintainers of other variant-specific channels
    forwards = {"nh343" : [],
                "nh363" : [],
                "nh370" : [],
                 "zapm" : [],
                   "gh" : [],
                  "dnh" : [],
                   "fh" : [],
                  "dyn" : [],
                  "nh4" : [],
                   "4k" : [],
                   "sp" : [],
                  "xnh" : [],
                  "spl" : [],
                "nh13d" : [],
                "slshm" : [],
                 "tnnt" : [],
               "nhthon" : [],
                 "ndnh" : [],
                "nndnh" : [],
                 "evil" : [],
                 "slth" : [],
                "gnoll" : [],
                  "ace" : [],
                "hackm" : [],
                 "nerf" : [],
                  "cre" : [],
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
                       "xnh" : "\x0309xnh\x03",
                       "spl" : "\x0303spl\x03",
                     "nh13d" : "\x0311nh13d\x03",
                     "slshm" : "\x0314slshm\x03",
                      "ndnh" : "\x0313ndnh\x03",
                     "nndnh" : "\x0313nndnh\x03",
                      "evil" : "\x0304evil\x03",
                      "tnnt" : "\x0310tnnt\x03",
                    "nhthon" : "\x0310nhthon\x03",
                        "un" : "\x0308un\x03",
                      "slth" : "\x0305slth\x03",
                     "gnoll" : "\x0309gnoll\x03",
                       "ace" : "\x0311ace\x03",
                     "hackm" : "\x0315hackm\x03",
                      "nerf" : "\x0308nerf\x03",
                       "cre" : "\x0311cre\x03",
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
                          INPR+"nh370.31-hdf/", INPR+"nh370.32-hdf/",
                          INPR+"nh370.35-hdf/", INPR+"nh370.36-hdf/",
                          INPR+"nh370.38-hdf/", INPR+"nh370.39-hdf/",
                          INPR+"nh370.40-hdf/", INPR+"nh370.42-hdf/",
                          INPR+"nh370.43-hdf/", INPR+"nh370.46-hdf/",
                          INPR+"nh370.47-hdf/", INPR+"nh370.50-hdf/",
                          INPR+"nh370.51-hdf/", INPR+"nh370.53-hdf/",
                          INPR+"nh370.58-hdf/", INPR+"nh370.59-hdf/",
                          INPR+"nh370.60-hdf/", INPR+"nh370.61-hdf/",
                          INPR+"nh370.62-hdf/", INPR+"nh370.64-hdf/",
                          INPR+"nh370.65-hdf/", INPR+"nh370.66-hdf/",
                          INPR+"nh370.69-hdf/", INPR+"nh370.70-hdf/",
                          INPR+"nh370.71-hdf/", INPR+"nh370.73-hdf/",
                          INPR+"nh370.78-hdf/", INPR+"nh370.80-hdf/",
                          INPR+"nh370.82-hdf/", INPR+"nh370.83-hdf/",
                          INPR+"nh370.84-hdf/", INPR+"nh370.86-hdf/",
                          INPR+"nh370.87-hdf/", INPR+"nh370.88-hdf/",
                          INPR+"nh370.89-hdf/", INPR+"nh370.90-hdf/",
                          INPR+"nh370.94-hdf/", INPR+"nh370.95-hdf/",
                          INPR+"nh370.97-hdf/", INPR+"nh370.101-hdf/",
                          INPR+"nh370.102-hdf/", INPR+"nh370.103-hdf/",
                          INPR+"nh370.105-hdf/", INPR+"nh370.106-hdf/",
                          INPR+"nh370.107-hdf/", INPR+"nh370.110-hdf/",
                          INPR+"nh370.112-hdf/", INPR+"nh370.114-hdf/",
                          INPR+"nh370.115-hdf/", INPR+"nh370.119-hdf/",
                          INPR+"nh370.120-hdf/", INPR+"nh370.123-hdf/",
                          INPR+"nh370.124-hdf/", INPR+"nh370.127-hdf/"],
                "zapm" : [INPR+"zapm/"],
                  "gh" : [INPR+"gh024/", INPR+"gh030/"],
                  "un" : [INPR+"un531/", INPR+"un532/",
                          INPR+"un600/", INPR+"un601/",
                          INPR+"un602/", INPR+"un603/",
                          INPR+"un604/", INPR+"un605/",
                          INPR+"un606/", INPR+"un607/",
                          INPR+"un608/", INPR+"un609/",
                          INPR+"un6010/", INPR+"un6011/",
                          INPR+"un6012/", INPR+"un6013/",
                          INPR+"un6014/"],
                 "dnh" : [INPR+"dnh3171/", INPR+"dnh318/",
                          INPR+"dnh319/", INPR+"dnh3191/",
                          INPR+"dnh320/", INPR+"dnh321/",
                          INPR+"dnh3211/", INPR+"dnh3212/",
                          INPR+"dnh3213/", INPR+"dnh3214/",
                          INPR+"dnh322/", INPR+"dnh323/",
                          INPR+"dnh324/"],
                  "fh" : [INPR+"fh/"],
                  "4k" : [INPR+"4k/", INPR+"4k4305/"],
                 "nh4" : [INPR+"nh4/"],
                  "sp" : [INPR+"sp065/", INPR+"sp070/"],
                 "xnh" : [INPR+"xnh040/", INPR+"xnh041/",
                          INPR+"xnh50/", INPR+"xnh51/",
                          INPR+"xnh51.1/", INPR+"xnh51.2/",
                          INPR+"xnh51.3/", INPR+"xnh600/",
                          INPR+"xnh610/", INPR+"xnh620/",
                          INPR+"xnh630/", INPR+"xnh700/",
                          INPR+"xnh7001/", INPR+"xnh710/",
                          INPR+"xnh800/", INPR+"xnh8001/",
                          INPR+"xnh900/"],
                 "spl" : [INPR+"spl063/", INPR+"spl064/",
                          INPR+"spl070/", INPR+"spl071/",
                          INPR+"spl071.21/", INPR+"spl080/",
                          INPR+"spl081/", INPR+"spl082/",
                          INPR+"spl100/", INPR+"spl110/",
                          INPR+"spl120/"],
               "nh13d" : [INPR+"nh13d/"],
               "slshm" : [INPR+"slashem/"],
                "ndnh" : [INPR+"ndnh-524/", INPR+"ndnh-1224/",
                          INPR+"ndnh-0416/", INPR+"ndnh-0521/",
                          INPR+"ndnh-0322/", INPR+"ndnh-0530/",
                          INPR+"ndnh-0918/", INPR+"ndnh-0515/",
                          INPR+"ndnh-0515v2/", INPR+"ndnh-0515v3/"],
               "nndnh" : [INPR+"nndnh-0515/", INPR+"nndnh-0516/"],
                "evil" : [INPR+"evil040/", INPR+"evil041/",
                          INPR+"evil042/", INPR+"evil050/",
                          INPR+"evil060/", INPR+"evil070/",
                          INPR+"evil071/", INPR+"evil080/",
                          INPR+"evil081/", INPR+"evil082/",
                          INPR+"evil083/", INPR+"evil084/",
                          INPR+"evil090/", INPR+"evil091/"],
                "tnnt" : [INPR+"tnnt/"],
              "nhthon" : [INPR+"nethackathon/"],
                "slth" : [INPR+"slth095/", INPR+"slth096/",
                          INPR+"slth097/"],
               "gnoll" : [INPR+"gnoll4104/", INPR+"gnoll410b2/",
                          INPR+"gnoll410b4/", INPR+"gnoll410b9/",
                          INPR+"gnoll410b14/", INPR+"gnoll410b15/",
                          INPR+"gnoll41041/", INPR+"gnoll410/",
                          INPR+"gnoll411/", INPR+"gnoll4123/",
                          INPR+"gnoll41316/", INPR+"gnoll41339/",
                          INPR+"gnoll41350/", INPR+"gnoll41352/",
                          INPR+"gnoll42016/", INPR+"gnoll42020/",
                          INPR+"gnoll42041/"],
                 "ace" : [INPR+"ace/"],
               "hackm" : [INPR+"hackem100/", INPR+"hackem110/",
                          INPR+"hackem114/", INPR+"hackem120/",
                          INPR+"hackem122/", INPR+"hackem130/",
                          INPR+"hackem131/", INPR+"hackem132/"],
                "nerf" : [INPR+"nerf200/", INPR+"nerf210/",
                          INPR+"nerf221/"],
                 "cre" : [INPR+"cre100/", INPR+"cre101/"],
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
                         FILEROOT+"nh370.32-hdf/var/whereis/",
                         FILEROOT+"nh370.35-hdf/var/whereis/",
                         FILEROOT+"nh370.36-hdf/var/whereis/",
                         FILEROOT+"nh370.38-hdf/var/whereis/",
                         FILEROOT+"nh370.39-hdf/var/whereis/",
                         FILEROOT+"nh370.40-hdf/var/whereis/",
                         FILEROOT+"nh370.42-hdf/var/whereis/",
                         FILEROOT+"nh370.43-hdf/var/whereis/",
                         FILEROOT+"nh370.46-hdf/var/whereis/",
                         FILEROOT+"nh370.47-hdf/var/whereis/",
                         FILEROOT+"nh370.50-hdf/var/whereis/",
                         FILEROOT+"nh370.51-hdf/var/whereis/",
                         FILEROOT+"nh370.53-hdf/var/whereis/",
                         FILEROOT+"nh370.58-hdf/var/whereis/",
                         FILEROOT+"nh370.59-hdf/var/whereis/",
                         FILEROOT+"nh370.60-hdf/var/whereis/",
                         FILEROOT+"nh370.61-hdf/var/whereis/",
                         FILEROOT+"nh370.62-hdf/var/whereis/",
                         FILEROOT+"nh370.64-hdf/var/whereis/",
                         FILEROOT+"nh370.65-hdf/var/whereis/",
                         FILEROOT+"nh370.66-hdf/var/whereis/",
                         FILEROOT+"nh370.69-hdf/var/whereis/",
                         FILEROOT+"nh370.70-hdf/var/whereis/",
                         FILEROOT+"nh370.71-hdf/var/whereis/",
                         FILEROOT+"nh370.73-hdf/var/whereis/",
                         FILEROOT+"nh370.78-hdf/var/whereis/",
                         FILEROOT+"nh370.80-hdf/var/whereis/",
                         FILEROOT+"nh370.82-hdf/var/whereis/",
                         FILEROOT+"nh370.83-hdf/var/whereis/",
                         FILEROOT+"nh370.84-hdf/var/whereis/",
                         FILEROOT+"nh370.86-hdf/var/whereis/",
                         FILEROOT+"nh370.87-hdf/var/whereis/",
                         FILEROOT+"nh370.88-hdf/var/whereis/",
                         FILEROOT+"nh370.89-hdf/var/whereis/",
                         FILEROOT+"nh370.90-hdf/var/whereis/",
                         FILEROOT+"nh370.94-hdf/var/whereis/",
                         FILEROOT+"nh370.95-hdf/var/whereis/",
                         FILEROOT+"nh370.97-hdf/var/whereis/",
                         FILEROOT+"nh370.101-hdf/var/whereis/",
                         FILEROOT+"nh370.102-hdf/var/whereis/",
                         FILEROOT+"nh370.103-hdf/var/whereis/",
                         FILEROOT+"nh370.105-hdf/var/whereis/",
                         FILEROOT+"nh370.106-hdf/var/whereis/",
                         FILEROOT+"nh370.107-hdf/var/whereis/",
                         FILEROOT+"nh370.110-hdf/var/whereis/",
                         FILEROOT+"nh370.112-hdf/var/whereis/",
                         FILEROOT+"nh370.114-hdf/var/whereis/",
                         FILEROOT+"nh370.115-hdf/var/whereis/",
                         FILEROOT+"nh370.119-hdf/var/whereis/",
                         FILEROOT+"nh370.120-hdf/var/whereis/",
                         FILEROOT+"nh370.123-hdf/var/whereis/",
                         FILEROOT+"nh370.124-hdf/var/whereis/",
                         FILEROOT+"nh370.127-hdf/var/whereis/"],
                  "gh": [FILEROOT+"grunthack-0.2.4/var/whereis/",
                         FILEROOT+"grunthack-0.3.0/var/whereis/"],
                 "dnh": [FILEROOT+"dnethack-3.17.1/whereis/",
                         FILEROOT+"dnethack-3.18.0/whereis/",
                         FILEROOT+"dnethack-3.19.0/whereis/",
                         FILEROOT+"dnethack-3.19.1/whereis/",
                         FILEROOT+"dnethack-3.20.0/whereis/",
                         FILEROOT+"dnethack-3.21.0/whereis/",
                         FILEROOT+"dnethack-3.21.1/whereis/",
                         FILEROOT+"dnethack-3.21.2/whereis/",
                         FILEROOT+"dnethack-3.21.3/whereis/",
                         FILEROOT+"dnethack-3.21.4/whereis/",
                         FILEROOT+"dnethack-3.22.0/whereis/",
                         FILEROOT+"dnethack-3.23.0/whereis/",
                         FILEROOT+"dnethack-3.24.0/whereis/"],
                  "fh": [FILEROOT+"fiqhackdir/data/"],
                 "dyn": [FILEROOT+"dynahack/dynahack-data/var/whereis/"],
                 "nh4": [FILEROOT+"nh4dir/save/whereis/"],
                  "4k": [FILEROOT+"fourkdir/save/",
                         FILEROOT+"fourkdir-4.3.0.5/save/"],
                  "sp": [FILEROOT+"sporkhack-0.6.5/var/",
                         FILEROOT+"sporkhack-0.7.0/var/"],
                 "xnh": [FILEROOT+"xnethack-0.4.0/var/whereis/",
                         FILEROOT+"xnethack-0.4.1/var/whereis/",
                         FILEROOT+"xnethack-5.0/var/whereis/",
                         FILEROOT+"xnethack-5.1/var/whereis/",
                         FILEROOT+"xnethack-5.1.1/var/whereis/",
                         FILEROOT+"xnethack-5.1.2/var/whereis/",
                         FILEROOT+"xnethack-5.1.3/var/whereis/",
                         FILEROOT+"xnethack-6.0.0/var/whereis/",
                         FILEROOT+"xnethack-6.1.0/var/whereis/",
                         FILEROOT+"xnethack-6.2.0/var/whereis/",
                         FILEROOT+"xnethack-6.3.0/var/whereis/",
                         FILEROOT+"xnethack-7.0.0/var/whereis/",
                         FILEROOT+"xnethack-7.0.0.1/var/whereis/",
                         FILEROOT+"xnethack-7.1.0/var/whereis/",
                         FILEROOT+"xnethack-8.0.0/var/whereis/",
                         FILEROOT+"xnethack-8.0.0.1/var/whereis/",
                         FILEROOT+"xnethack-9.0.0/var/whereis/"],
                 "spl": [FILEROOT+"splicehack-0.6.3/var/whereis/",
                         FILEROOT+"splicehack-0.6.4/var/whereis/",
                         FILEROOT+"splicehack-0.7.0/var/whereis/",
                         FILEROOT+"splicehack-0.7.1/var/whereis/",
                         FILEROOT+"splicehack-0.7.1-21/var/whereis/",
                         FILEROOT+"splicehack-0.8.0/var/whereis/",
                         FILEROOT+"splicehack-0.8.1/var/whereis/",
                         FILEROOT+"splicehack-0.8.2/var/whereis/",
                         FILEROOT+"splicehack-1.0.0/var/whereis/",
                         FILEROOT+"splicehack-1.1.0/var/whereis/",
                         FILEROOT+"splicehack-1.2.0/var/whereis/"],
               "nh13d": [FILEROOT+"nh13d/whereis/"],
               "slshm": [FILEROOT+"slashem-0.0.8E0F2/whereis/"],
                "ndnh": [FILEROOT+"notdnethack-2019.05.24/whereis/",
                         FILEROOT+"notdnethack-2019.12.24/whereis/",
                         FILEROOT+"notdnethack-2020.04.16/whereis/",
                         FILEROOT+"notdnethack-2021.05.21/whereis/",
                         FILEROOT+"notdnethack-2022.03.22/whereis/",
                         FILEROOT+"notdnethack-2022.05.30/whereis/",
                         FILEROOT+"notdnethack-2022.09.18/whereis/",
                         FILEROOT+"notdnethack-2023.05.15/whereis/",
                         FILEROOT+"notdnethack-2024.05.15/whereis/",
                         FILEROOT+"notdnethack-2025.05.15/whereis/"],
               "nndnh": [FILEROOT+"notnotdnethack-2024.05.15/whereis/",
                         FILEROOT+"notnotdnethack-2025.05.16/whereis/"],
                "evil": [FILEROOT+"evilhack-0.4.0/var/whereis/",
                         FILEROOT+"evilhack-0.4.1/var/whereis/",
                         FILEROOT+"evilhack-0.4.2/var/whereis/",
                         FILEROOT+"evilhack-0.5.0/var/whereis/",
                         FILEROOT+"evilhack-0.6.0/var/whereis/",
                         FILEROOT+"evilhack-0.7.0/var/whereis/",
                         FILEROOT+"evilhack-0.7.1/var/whereis/",
                         FILEROOT+"evilhack-0.8.0/var/whereis/",
                         FILEROOT+"evilhack-0.8.1/var/whereis/",
                         FILEROOT+"evilhack-0.8.2/var/whereis/",
                         FILEROOT+"evilhack-0.8.3/var/whereis/",
                         FILEROOT+"evilhack-0.8.4/var/whereis/",
                         FILEROOT+"evilhack-0.9.0/var/whereis/",
                         FILEROOT+"evilhack-0.9.1/var/whereis/"],
                "tnnt": [FILEROOT+"tnnt/var/whereis/"],
              "nhthon": [FILEROOT+"nethackathon/var/whereis/"],
                "slth": [FILEROOT+"slashthem-0.9.5/whereis/",
                         FILEROOT+"slashthem-0.9.6/whereis/",
                         FILEROOT+"slashthem-0.9.7/whereis/"],
               "gnoll": [FILEROOT+"gnollhack-4.1.2.3/var/whereis/",
                         FILEROOT+"gnollhack-4.1.3.16/var/whereis/",
                         FILEROOT+"gnollhack-4.1.3.39/var/whereis/",
                         FILEROOT+"gnollhack-4.1.3.50/var/whereis/",
                         FILEROOT+"gnollhack-4.1.3.52/var/whereis/",
                         FILEROOT+"gnollhack-4.2.0.16/var/whereis/",
                         FILEROOT+"gnollhack-4.2.0.20/var/whereis/",
                         FILEROOT+"gnollhack-4.2.0.41/var/whereis/"],
               "hackm": [FILEROOT+"hackem-1.0.0/var/whereis/",
                         FILEROOT+"hackem-1.1.0/var/whereis/",
                         FILEROOT+"hackem-1.1.4/var/whereis/",
                         FILEROOT+"hackem-1.2.0/var/whereis/",
                         FILEROOT+"hackem-1.2.2/var/whereis/",
                         FILEROOT+"hackem-1.3.0/var/whereis/",
                         FILEROOT+"hackem-1.3.1/var/whereis/",
                         FILEROOT+"hackem-1.3.2/var/whereis/"],
                "nerf": [FILEROOT+"nerfhack-2.0.0/var/whereis/",
                         FILEROOT+"nerfhack-2.1.0/var/whereis/",
                         FILEROOT+"nerfhack-2.2.1/var/whereis/"],
                 "cre": [FILEROOT+"crecellehack-1.0.0/var/whereis/",
                         FILEROOT+"crecellehack-1.0.1/var/whereis/"],
                  "un": [FILEROOT+"un531/var/unnethack/",
                         FILEROOT+"un532/var/unnethack/",
                         FILEROOT+"unnethack-6.0.0/var/unnethack/",
                         FILEROOT+"unnethack-6.0.1/var/unnethack/",
                         FILEROOT+"unnethack-6.0.2/var/unnethack/",
                         FILEROOT+"unnethack-6.0.3/var/unnethack/",
                         FILEROOT+"unnethack-6.0.4/var/unnethack/",
                         FILEROOT+"unnethack-6.0.5/var/unnethack/",
                         FILEROOT+"unnethack-6.0.6/var/unnethack/",
                         FILEROOT+"unnethack-6.0.7/var/whereis/",
                         FILEROOT+"unnethack-6.0.8/var/whereis/",
                         FILEROOT+"unnethack-6.0.9/var/whereis/",
                         FILEROOT+"unnethack-6.0.10/var/whereis/",
                         FILEROOT+"unnethack-6.0.11/var/whereis/",
                         FILEROOT+"unnethack-6.0.12/var/whereis/",
                         FILEROOT+"unnethack-6.0.13/var/whereis/",
                         FILEROOT+"unnethack-6.0.14/var/whereis/"]}

    dungeons = {"nh343": ["The Dungeons of Doom","Gehennom","The Gnomish Mines","The Quest",
                          "Sokoban","Fort Ludios","Vlad's Tower","The Elemental Planes"],
                "nh363": ["The Dungeons of Doom","Gehennom","The Gnomish Mines","The Quest",
                          "Sokoban","Fort Ludios","Vlad's Tower","The Elemental Planes"],
                "nh370": ["The Dungeons of Doom","Gehennom","The Gnomish Mines","The Quest",
                          "Sokoban","Fort Ludios","Vlad's Tower","The Elemental Planes",
                          "The Tutorial"],
                   "gh": ["The Dungeons of Doom","Gehennom","The Gnomish Mines","The Quest",
                          "Sokoban","Fort Ludios","Vlad's Tower","The Elemental Planes"],
                  "dnh": ["The Dungeons of Doom","Gehennom","The Gnomish Mines","Law Quest",
                          "Neutral Quest","The Lost Cities","Chaos Quest","The Quest",
                          "Sokoban","Fort Ludios","The Lost Tomb","The Sunless Sea",
                          "The Temple of Moloch","The Dispensary","Vlad's Tower",
                          "The Elemental Planes"],
                 "ndnh": ["The Dungeons of Doom","Gehennom","Nowhere","The Collapsed Mineshaft",
                          "The Gnomish Mines","The Ice Caves","The Black Forest","The Dismal Swamp",
                          "The Archipelago","Law Quest","Neutral Quest","The Lost Cities","Chaos Quest",
                          "The Quest","Lokoban","Fort Ludios","The Void","Sacristy","The Lost Tomb",
                          "The Sunless Sea","The Temple of Moloch","The Dispensary","The Spire",
                          "Vlad's Tower","The Elemental Planes"],
                "nndnh": ["The Dungeons of Doom","Gehennom","Nowhere","The Collapsed Mineshaft",
                          "The Gnomish Mines","The Ice Caves","The Black Forest","The Dismal Swamp",
                          "The Archipelago","Law Quest","Neutral Quest","The Lost Cities","Chaos Quest",
                          "The Quest","Lokoban","Fort Ludios","The Void","Sacristy","The Lost Tomb",
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
                  "xnh": ["The Dungeons of Doom","Gehennom","The Gnomish Mines","The Quest",
                          "Sokoban","Fort Ludios","Vlad's Tower","Cocytus","Asphodel","Shedaklah",
                          "The Citadel of Dis","The Abyss","Tartarus","The Wizard's Tower",
                          "The Elemental Planes","The Tutorial"],
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
                          "The Quest","Sokoban","Town","Grund's Stronghold","Fort Ludios","The Wyrm Caves",
                          "One-eyed Sam's Market","The Lost Tomb","The Spider Caves","The Sunless Sea",
                          "The Temple of Moloch","The Giant Caverns","Vlad's Tower","Frankenstein's Lab",
                          "The Elemental Planes"],
                 "tnnt": ["The Dungeons of Doom","Gehennom","The Gnomish Mines","The Quest",
                          "Sokoban","Fort Ludios","DevTeam's Office","Deathmatch Arena",
                          "Vlad's Tower","The Elemental Planes"],
               "nhthon": ["The Dungeons of Doom","Gehennom","The Gnomish Mines","The Quest",
                          "Sokoban","Fort Ludios","DevTeam's Office","Deathmatch Arena",
                          "Vlad's Tower","The Elemental Planes"],
                 "evil": ["The Dungeons of Doom","Gehennom","The Gnomish Mines","Goblin Town",
                          "The Quest","Sokoban","Fort Ludios","The Ice Queen's Realm","The Hidden Dungeon",
                          "Vecna's Domain","Vlad's Tower","Purgatory","The Wizard's Tower",
                          "The Elemental Planes"],
                "gnoll": ["The Dungeons of Doom","Gehennom","The Gnomish Mines","The Large Circular Dungeon",
                          "The Quest","Sokoban","Fort Ludios","Plane of the Modron","Hellish Pastures",
                          "Vlad's Tower","The Elemental Planes"],
                  "ace": ["The Dungeons of Doom","Gehennom","The Gnomish Mines","The Quest",
                          "Sokoban","Fort Ludios","Vlad's Tower","The Elemental Planes"],
                "hackm": ["The Dungeons of Doom","Gehennom","The Gnomish Mines","The Quest",
                          "Sokoban","Town","Grund's Stronghold","Fort Ludios","The Wyrm Caves",
                          "One-eyed Sam's Market","The Lost Levels","The Temple of Moloch","Vecna's Domain",
                          "Vlad's Tower","The Elemental Planes"],
                   "un": ["The Dungeons of Doom","Gehennom","Sheol","The Gnomish Mines",
                          "The Quest","Sokoban","Town","The Ruins of Moria","Fort Ludios",
                          "One-eyed Sam's Market","Vlad's Tower","The Dragon Caves",
                          "The Elemental Planes","Advent Calendar"],
                 "nerf": ["The Dungeons of Doom","Gehennom","The Gnomish Mines","The Quest","Sokoban",
                          "Fort Ludios","The Lost Tomb","The Wyrm Caves","The Temple of Moloch",
                          "Vlad's Tower","The Wizard's Tower","The Elemental Planes","The Tutorial"],
                  "cre": ["The Dungeons of Doom","Gehennom","The Gnomish Mines","The Quest",
                          "Sokoban","Fort Ludios","Vlad's Tower","The Elemental Planes",
                          "The Tutorial"]}

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
        # NetHack 1.3d Vanilla has a role of 'Elf' as well as 'Fighter' and 'Ninja' (the latter already included below from SlashTHEM roles)
        "elf": "elf",
        "fig": "fighter",
        # Dnh, includes all of vanilla
        "ana": "anachrononaut",
        "bin": "binder",
        "nob": "noble",
        "pir": "pirate",
        "brd": "troubadour",
        "con": "convict",
        "mad": "madman",
        # Ndnh, includes all of vanilla and dnh
        "acu": "illithanachronounbinder",
        # Nndnh, includes all of vanilla and dnh, ndnh
        "oct": "octopode",
        # SpliceHack, includes all of vanilla
        "car": "cartomancer",
        "dra": "dragon rider",
        # Evilhack, includes all of vanilla
        "inf": "infidel",
        "dru": "druid",
        # SLASH'EM/SlashTHEM/HackEM
        "und": "undead slayer",
        "fla": "flame mage",
        "ice": "ice mage",
        "nec": "necromancer",
        "yeo": "yeoman",
        "jed": "jedi",
        "nin": "ninja",
        "unt": "undertaker",
        "pal": "paladin",
        "loc": "locksmith",
        "cor": "corsair",
        "chf": "chef",
        "fir": "firefighter",
        "off": "officer",
        "ele": "electric mage",
        "aci": "acid mage",
        "hac": "hacker",
        "gee": "geek",
        "drk": "drunk",
        "gla": "gladiator",
        "div": "diver",
        "lun": "lunatic",
        "mus": "musician",
        "zoo": "zookeeper",
        # CrecelleHack
        "wre": "wrestler",
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
        "and": "android",
        # Ndnh, includes all of vanilla and dnh
        "sal": "salamander",
        "eth": "etherealoid",
        "ent": "treant",
        # 4k, includes vanilla
        "scu": "scurrier",
        "syl": "sylph",
        #SpliceHack, includes vanilla
        "inf": "infernal",
        "mer": "merfolk",
        #EvilHack, includes vanilla
        "cen": "centaur",
        "hob": "hobbit",
        "ith": "illithid",
        "trt": "tortle",
        "dra": "draugr",
        #SLASH'EM/Hack'EM
        "dop": "doppelganger",
        "lyc": "lycanthrope",
        #SlashTHEM
        "ill": "illithid",
        "nym": "nymph",
        "tro": "troll",
        "gul": "ghoul",
        #NerfHack
        "gru": "grung",
    }
    # save typing these out in multiple places
    vanilla_roles = ["arc","bar","cav","hea","kni","mon","pri",
                     "ran","rog","sam","tou","val","wiz"]
    vanilla_races = ["dwa","elf","gno","hum","orc"]

    # varname: ([aliases],[roles],[races],"github org/role/mainbranch[/subdirs]")
    # first alias will be used for !variant
    # note this breaks if a player has the same name as an alias
    # so don't do that (I'm looking at you, FIQ)
    # the github string is used for rumors:
    # https://raw.githubusercontent.com/[YOUR STRING HERE]/dat/rumors.fal
    # should be a valid url
    variants = {"nh343": (["nh343", "nethack", "343"],
                          vanilla_roles, vanilla_races,
                          "NHTangles/NetHack/hardfought"),
                "nh363": (["nh363", "363", "363-hdf"],
                          vanilla_roles, vanilla_races,
                          None),
                "nh370": (["nh370", "370", "370-hdf"],
                          vanilla_roles, vanilla_races,
                          "NetHack/NetHack/NetHack-3.7"),
                "nh13d": (["nh13d", "13d"],
                          vanilla_roles + ["elf", "fig", "nin"], None,
                          None), # special hardcoded case because it doesn't behave like the rest
                  "nh4": (["nethack4", "n4"],
                          vanilla_roles, vanilla_races,
                          "NHTangles/nethack4/master/libnethack"),
                   "gh": (["grunthack", "grunt"],
                          vanilla_roles, vanilla_races + ["gia", "kob", "ogr"],
                          "NHTangles/GruntHack/master"),
                  "dnh": (["dnethack", "dn"],
                          vanilla_roles
                            + ["ana", "bin", "nob", "pir", "brd", "con", "mad"],
                          vanilla_races
                            + ["clk", "con", "bat", "dro", "hlf", "inc", "vam", "swn", "and"],
                          "Chris-plus-alphanumericgibberish/dNAO/compat-3.22.0"), # not ideal...
                 "ndnh": (["notdnethack", "ndn"],
                          vanilla_roles
                            + ["ana", "bin", "nob", "pir", "brd", "con", "mad", "acu"],
                          vanilla_races
                            + ["clk", "con", "bat", "dro", "hlf", "inc", "vam", "swn", "and", "sal", "eth", "ent"],
                          "demogorgon22/notdnethack/master"),
                "nndnh": (["notnotdnethack", "nnd"],
                          vanilla_roles
                            + ["ana", "bin", "nob", "pir", "brd", "con", "mad", "acu"],
                          vanilla_races
                            + ["clk", "con", "bat", "dro", "hlf", "inc", "vam", "swn", "and", "sal", "eth", "ent", "oct"],
                          "k21971/notnotdnethack/master"),
                   "un": (["unnethack", "unh"],
                          vanilla_roles + ["con"], vanilla_races,
                          "unnethack/unnethack/master"),
                  "xnh": (["xnethack", "xnh"],
                          vanilla_roles, vanilla_races,
                          "copperwater/xNetHack/master"),
                  "spl": (["splicehack", "splice", "spl"],
                          vanilla_roles + ["con", "pir", "car", "dra"], vanilla_races + ["vam", "inf", "mer"],
                          "NullCGT/SpliceHack/Master"),
                  "dyn": (["dynahack", "dyna"],
                          vanilla_roles + ["con"], vanilla_races + ["vam"],
                          "tung/DynaHack/unnethack/libnitrohack"), # ???
                   "fh": (["fiqhack"], # not "fiq" see comment above
                          vanilla_roles, vanilla_races,
                          "FredrIQ/fiqhack/development/libnethack"),
                   "sp": (["sporkhack", "spork"],
                          vanilla_roles, vanilla_races,
                          "NHTangles/sporkhack/master"),
                   "4k": (["nhfourk", "nhf", "fourk"],
                          vanilla_roles, vanilla_races + ["gia", "scu", "syl"],
                          "tsadok/nhfourk/master/libnethack"),
                "slshm": (["slash", "slash'em", "slshm"],
                          vanilla_roles + ["fla", "ice", "nec", "und", "yeo"],
                          vanilla_races + ["dop", "dro", "hob", "lyc", "vam"],
                          "k21971/SlashEM/master"),
                 "slth": (["slashthem", "slth"],
                          vanilla_roles + ["fla", "ice", "nec", "und", "yeo", "jed",
                                           "nin", "unt", "pal", "loc", "cor", "chf",
                                           "fir", "off", "ele", "aci", "hac", "gee",
                                           "drk", "gla", "div", "lun", "mus", "zoo"],
                          vanilla_races + ["dop", "dro", "hob", "lyc", "vam", "ill", "nym", "tro", "gul"],
                          "k21971/SlashTHEM/master"),
                 "tnnt": (["tnnt"],
                          vanilla_roles, vanilla_races,
                          None), # no different from vanilla
               "nhthon": (["nethackathon", "nhthon"],
                          vanilla_roles, vanilla_races,
                          None), # no different from vanilla
                 "evil": (["evilhack", "evil", "evl"],
                          vanilla_roles + ["con", "inf", "dru"],
                          vanilla_races + ["cen", "gia", "hob", "ith", "trt", "dro", "dra", "vam"],
                          "k21971/EvilHack/master"),
                  "ace": (["ace"],
                          vanilla_roles, vanilla_races,
                          None), # no different from vanilla
                "hackm": (["hackem", "hackm"],
                          vanilla_roles + ["con", "inf", "fla", "ice", "nec", "und", "yeo", "jed", "pir"],
                          vanilla_races + ["cen", "gia", "hob", "ith", "trt", "vam", "dop"],
                          "nethack-cleaner/HackEM/master"),
                 "nerf": (["nerf", "nerfhack"],
                          vanilla_roles + ["car", "und"],
                          vanilla_races + ["vam", "gru"],
                          "elunna/NerfHack/master"),
                  "cre": (["cre", "crecellehack"],
                          vanilla_roles + ["wre"],
                          vanilla_races,
                          "NullCGT/CrecelleHack/main"), # no different from vanilla
                "gnoll": (["gnoll", "gnollhack"],
                          vanilla_roles, vanilla_races,
                          "hyvanmielenpelit/GnollHack/master")}

    # variants which support streaks.
    streakvars = ["nh343", "nh363", "nh370", "nh13d", "gh", "dnh", "un", "sp", "xnh", "spl", "slshm", "tnnt", "nhthon", "ndnh", "evil", "slth", "ace", "gnoll", "hackm", "nndnh", "nerf", "cre"]
    # for !asc statistics - assume these are the same for all variants, or at least the sane ones.
    aligns = ["Law", "Neu", "Cha", "Una", "Non"]
    genders = ["Mal", "Fem", "Nbn"]

    #who is making tea? - bots of the nethack community who have influenced this project.
    brethren = ["Rodney", "Athame", "Arsinoe", "Izchak", "TheresaMayBot", "FCCBot", "the late Pinobot", "Announcy", "demogorgon", "the /dev/null/oracle", "NotTheOracle\\dnt", "Croesus", "Hecubus", "Yendor"]
    looping_calls = None

    # SASL auth nonsense required if we run on AWS
    # copied from https://github.com/habnabit/txsocksx/blob/master/examples/tor-irc.py
    # irc_CAP and irc_9xx are UNDOCUMENTED.
    def connectionMade(self):
        # Skip SASL for testing if configured
        try:
            from test_botconf import DISABLE_SASL
            if DISABLE_SASL:
                irc.IRCClient.connectionMade(self)
                return
        except ImportError:
            pass

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
        """Called when bot successfully connects to IRC"""
        self.factory.resetDelay()
        self.startHeartbeat()
        self.sendLine('MODE {} -R'.format(self.nickname))
        if not SLAVE: self.join(CHANNEL)
        random.seed()

        # Track bot start time for uptime calculation
        self.starttime = time.time()

        self._initializeLogs()
        self._initializeGameTracking()
        self._initializeStreaks()
        self._initializeAscensions()
        self._initializeDatabases()
        self._initializeCommands()
        self._initializeRateLimiting()
        self._seekToEndOfLivelogs()
        self._populateHistoricalData()
        self._startMonitoringTasks()

    def _initializeLogs(self):
        """Initialize log file tracking"""
        self.logs = {}
        for xlogfile, (variant, delim, dumpfmt) in self.xlogfiles.items():
            self.logs[xlogfile] = (self.xlogfileReport, variant, delim, dumpfmt)
        for livelog, (variant, delim) in self.livelogs.items():
            self.logs[livelog] = (self.livelogReport, variant, delim, "")

        self.logs_seek = {}
        self.looping_calls = {}

    def _initializeGameTracking(self):
        """Initialize last game tracking"""
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

    def _initializeStreaks(self):
        """Initialize streak tracking"""
        self.curstreak = {}
        self.longstreak = {}
        for v in self.streakvars:
            # curstreak[var][player] = (start, end, length)
            self.curstreak[v] = {}
            # longstreak - as above
            self.longstreak[v] = {}

    def _initializeAscensions(self):
        """Initialize ascension tracking"""
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
        for v in self.variants:
            self.asc[v] = {};
            self.allgames[v] = {};

    def _initializeDatabases(self):
        """Initialize shelve databases"""
        # for !tell
        try:
            self.tellbuf = shelve.open(BOTDIR + "/tellmsg.db", writeback=False)
        except (OSError, IOError):
            self.tellbuf = shelve.open(BOTDIR + "/tellmsg", writeback=False, protocol=2)

        # for !setmintc
        try:
            self.plr_tc = shelve.open(BOTDIR + "/plrtc.db", writeback=False)
        except (OSError, IOError):
            self.plr_tc = shelve.open(BOTDIR + "/plrtc", writeback=False, protocol=2)

    def _initializeCommands(self):
        """Initialize command mappings"""

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
                         "rumor"    : self.doRumor,
                         "rumour"   : self.doRumor,
                         "status"   : self.doStatus,
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

    def _initializeRateLimiting(self):
        """Initialize rate limiting tracking with crash-safe defaults"""
        try:
            # Simple time-window per user approach
            self.rate_limits = {}

            # Abuse detection: track consecutive command patterns
            self.abuse_penalties = {}  # user -> penalty_end_time
            self.consecutive_commands = {}  # user -> [timestamp, timestamp, ...]

            # Response rate limiting: prevent penalty message spam
            self.penalty_responses = {}  # user -> [timestamp, timestamp, ...]

            # Burst protection: prevent multiple commands per second
            self.last_command_time = {}  # user -> timestamp of last command

            print("Rate limiting initialized successfully")
        except Exception as e:
            print(f"Warning: Rate limiting initialization failed: {e}")
            # Ensure safe defaults even if initialization fails
            self.rate_limits = {}
            self.abuse_penalties = {}
            self.consecutive_commands = {}
            self.penalty_responses = {}
            self.last_command_time = {}

    def _checkRateLimit(self, sender, command):
        """
        Check if user is rate limited for this command.
        Returns True if command should be allowed, False if rate limited.
        Uses fail-safe approach - if anything breaks, allow the command.
        """
        try:
            # Apply rate limiting to all users (including admins for security)
            # Get current time
            now = time.time()

            # Check if user is currently under abuse penalty
            if sender in self.abuse_penalties:
                if now < self.abuse_penalties[sender]:
                    return False  # Still under penalty
                else:
                    # Penalty expired, clean up
                    del self.abuse_penalties[sender]
                    self.consecutive_commands[sender] = []

            # Clean up old entries first (older than rate limit window)
            if sender in self.rate_limits:
                self.rate_limits[sender] = [
                    timestamp for timestamp in self.rate_limits[sender]
                    if now - timestamp < RATE_LIMIT_WINDOW
                ]

                # Remove empty entries
                if not self.rate_limits[sender]:
                    del self.rate_limits[sender]

            # Initialize user's rate limit tracking if needed
            if sender not in self.rate_limits:
                self.rate_limits[sender] = []

            # Apply rate limit to all commands
            limit = RATE_LIMIT_COMMANDS

            # Check if user has exceeded rate limit
            if len(self.rate_limits[sender]) >= limit:
                return False  # Rate limited

            # Record this command attempt
            self.rate_limits[sender].append(now)

            # Track commands for abuse detection (timestamp-based)
            if sender not in self.consecutive_commands:
                self.consecutive_commands[sender] = []

            # Clean up old command timestamps (older than abuse window)
            self.consecutive_commands[sender] = [
                timestamp for timestamp in self.consecutive_commands[sender]
                if now - timestamp < ABUSE_WINDOW
            ]

            # Record this command
            self.consecutive_commands[sender].append(now)

            # Check for abuse pattern (too many commands in time window)
            if len(self.consecutive_commands[sender]) >= ABUSE_THRESHOLD:
                # Impose abuse penalty
                self.abuse_penalties[sender] = now + ABUSE_PENALTY
                self.consecutive_commands[sender] = []
                print(f"Abuse penalty imposed on {sender}: spamming consecutive commands")
                return False  # Block this command and future commands for penalty period

            return True  # Allow command

        except Exception as e:
            print(f"Rate limiting error for {sender}: {e}")
            # Fail-safe: allow command if rate limiting breaks
            return True

    def _shouldSendPenaltyMessage(self, sender):
        """
        Check if we should send a penalty message to prevent spam.
        Returns True if message should be sent, False if user is sending too many.
        """
        try:
            now = time.time()

            # Clean up old penalty response timestamps
            if sender in self.penalty_responses:
                self.penalty_responses[sender] = [
                    timestamp for timestamp in self.penalty_responses[sender]
                    if now - timestamp < RESPONSE_RATE_WINDOW
                ]

                # Remove empty entries
                if not self.penalty_responses[sender]:
                    del self.penalty_responses[sender]

            # Initialize if needed
            if sender not in self.penalty_responses:
                self.penalty_responses[sender] = []

            # Check if user has exceeded penalty response limit
            if len(self.penalty_responses[sender]) >= RESPONSE_RATE_LIMIT:
                return False  # Don't send penalty message (would be spam)

            # Record this penalty response
            self.penalty_responses[sender].append(now)
            return True  # Send penalty message

        except Exception as e:
            print(f"Penalty response check error for {sender}: {e}")
            # Fail-safe: allow message
            return True

    def _checkBurstProtection(self, sender, command):
        """
        Check burst protection: only allow 1 command per time window.
        Returns True if command should be allowed, False if it's a burst (silently ignore).
        """
        try:
            now = time.time()

            # Apply burst protection to all commands
            burst_window = BURST_WINDOW

            # Check if user sent a command too recently
            if sender in self.last_command_time:
                time_since_last = now - self.last_command_time[sender]
                if time_since_last < burst_window:
                    return False  # Silently ignore burst commands

            # Record this command time for burst protection
            self.last_command_time[sender] = now
            return True  # Allow command

        except Exception as e:
            print(f"Burst protection error for {sender}: {e}")
            # Fail-safe: allow command
            return True

    def _seekToEndOfLivelogs(self):
        """Seek to end of livelog files"""

        # Skip file operations in test mode
        if TEST:
            print("Skipping livelog initialization in test mode")
            return

        # seek to end of livelogs
        for filepath in self.livelogs:
            with filepath.open("r") as handle:
                handle.seek(0, 2)
                self.logs_seek[filepath] = handle.tell()

    def _populateHistoricalData(self):
        """Read xlogfiles to populate historical game data"""

        # Skip file operations in test mode
        if TEST:
            print("Skipping historical data population in test mode")
            return

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

    def _startMonitoringTasks(self):
        """Start periodic monitoring tasks"""

        # Skip file monitoring in test mode
        if TEST:
            print("Skipping file monitoring tasks in test mode")
            self.looping_calls = {}
            # Still start nick check and cleanup tasks
            self.looping_calls["nick"] = task.LoopingCall(self.nickCheck)
            self.looping_calls["nick"].start(30)

            self.looping_calls["cleanup"] = task.LoopingCall(self.cleanupOldData)
            self.looping_calls["cleanup"].start(3600)
            return

        # poll logs for updates every LOG_CHECK_INTERVAL seconds
        for filepath in self.logs:
            self.looping_calls[filepath] = task.LoopingCall(self.logReport, filepath)
            self.looping_calls[filepath].start(LOG_CHECK_INTERVAL)

        # Additionally, keep an eye on our nick to make sure it's right.
        # Perhaps we only need to set this up if the nick was originally
        # in use when we signed on, but a 30-second looping call won't kill us
        self.looping_calls["nick"] = task.LoopingCall(self.nickCheck)
        self.looping_calls["nick"].start(30)

        # Cleanup old data periodically (every hour)
        self.looping_calls["cleanup"] = task.LoopingCall(self.cleanupOldData)
        self.looping_calls["cleanup"].start(3600)

    def nickCheck(self):
        # also rejoin the channel here, in case we drop off for any reason
        if not SLAVE: self.join(CHANNEL)
        if (self.nickname != NICK):
            self.setNick(NICK)

    def cleanupOldData(self):
        """Clean up old undelivered messages and limit cache sizes"""
        now = time.time()

        # Clean up undelivered !tell messages older than 180 days
        try:
            old_recipients = []
            for recipient in self.tellbuf:
                messages = self.tellbuf[recipient]
                # Filter out messages older than 180 days
                new_messages = [(fwd, sender, ts, msg) for (fwd, sender, ts, msg) in messages
                               if now - ts < 180 * 24 * 3600]
                if new_messages != messages:
                    if new_messages:
                        self.tellbuf[recipient] = new_messages
                    else:
                        old_recipients.append(recipient)

            # Delete empty entries
            for recipient in old_recipients:
                del self.tellbuf[recipient]

            if old_recipients:
                self.tellbuf.sync()
                print(f"Cleaned up old messages for {len(old_recipients)} recipients")
        except Exception as e:
            print(f"Error cleaning up tellbuf: {e}")

        # Clean up stale queries older than 1 hour (in case timeoutQuery failed)
        try:
            stale_queries = []
            for query_id in list(self.queries.keys()):
                # If query lacks timestamp, assume it's stale
                if "timestamp" not in self.queries[query_id]:
                    stale_queries.append(query_id)
                elif now - self.queries[query_id].get("timestamp", 0) > 3600:
                    stale_queries.append(query_id)

            for query_id in stale_queries:
                self.queries.pop(query_id, None)

            if stale_queries:
                print(f"Cleaned up {len(stale_queries)} stale queries")
        except Exception as e:
            print(f"Error cleaning up queries: {e}")

        # Limit rumor cache to 50 most recent entries
        if len(self.rumorCache) > 50:
            # Sort by timestamp and keep newest 50
            sorted_items = sorted(self.rumorCache.items(), key=lambda x: x[1][0], reverse=True)
            self.rumorCache = dict(sorted_items[:50])
            print(f"Trimmed rumor cache to 50 entries")

        # Clean up old rate limiting entries
        try:
            users_to_clean = []
            for user in list(self.rate_limits.keys()):
                # Remove timestamps older than rate limit window
                self.rate_limits[user] = [
                    timestamp for timestamp in self.rate_limits[user]
                    if now - timestamp < RATE_LIMIT_WINDOW
                ]
                # Remove empty entries
                if not self.rate_limits[user]:
                    users_to_clean.append(user)

            for user in users_to_clean:
                del self.rate_limits[user]

            if users_to_clean:
                print(f"Cleaned up rate limiting for {len(users_to_clean)} users")
        except Exception as e:
            print(f"Error cleaning up rate limits: {e}")

        # Clean up expired abuse penalties and reset old consecutive counters
        try:
            expired_penalties = []
            for user in list(self.abuse_penalties.keys()):
                if now >= self.abuse_penalties[user]:
                    expired_penalties.append(user)

            for user in expired_penalties:
                del self.abuse_penalties[user]
                self.consecutive_commands[user] = []

            # Clean up very old consecutive command timestamps (>24 hours inactive)
            old_consecutive = []
            for user in list(self.consecutive_commands.keys()):
                if self.consecutive_commands[user]:
                    # Check if all timestamps are older than 24 hours
                    latest_command = max(self.consecutive_commands[user])
                    if now - latest_command > 86400:  # 24 hours
                        old_consecutive.append(user)
                else:
                    # Empty list, can be cleaned up
                    old_consecutive.append(user)

            for user in old_consecutive:
                self.consecutive_commands.pop(user, None)

            if expired_penalties or old_consecutive:
                print(f"Cleaned up abuse tracking: {len(expired_penalties)} expired penalties, {len(old_consecutive)} old counters")
        except Exception as e:
            print(f"Error cleaning up abuse tracking: {e}")

        # Clean up old penalty response tracking
        try:
            penalty_users_to_clean = []
            for user in list(self.penalty_responses.keys()):
                # Remove timestamps older than penalty response window
                self.penalty_responses[user] = [
                    timestamp for timestamp in self.penalty_responses[user]
                    if now - timestamp < RESPONSE_RATE_WINDOW
                ]
                # Remove empty entries
                if not self.penalty_responses[user]:
                    penalty_users_to_clean.append(user)

            for user in penalty_users_to_clean:
                del self.penalty_responses[user]

            if penalty_users_to_clean:
                print(f"Cleaned up penalty response tracking for {len(penalty_users_to_clean)} users")
        except Exception as e:
            print(f"Error cleaning up penalty responses: {e}")

        # Clean up old last command time tracking (older than 24 hours)
        try:
            old_command_times = []
            for user in list(self.last_command_time.keys()):
                if now - self.last_command_time[user] > 86400:  # 24 hours
                    old_command_times.append(user)

            for user in old_command_times:
                del self.last_command_time[user]

            if old_command_times:
                print(f"Cleaned up old command time tracking for {len(old_command_times)} users")
        except Exception as e:
            print(f"Error cleaning up command times: {e}")

    def nickChanged(self, nn):
        # catch successful changing of nick from above and identify with nickserv
        self.msg("NickServ", "identify " + nn + " " + self.password)

    #helper functions
    #lookup canonical variant id from alias
    def varalias(self,alias):
        alias = alias.lower()
        if alias in self.variants: return alias
        for v in self.variants:
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
        message = RE_COLOR_FG_BG.sub('', msg) # fg,bg pair
        message = RE_COLOR_FG.sub('', message) # fg only
        message = RE_COLOR_END.sub('', message) # end of colour and italics
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

            # In test mode, respond immediately to avoid delays
            if TEST and sender == NICK:
                self.queries[msgwords[1]]["callback"](self.queries.pop(msgwords[1]))
            elif set(self.queries[msgwords[1]]["resp"]) >= set(self.slaves):
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
            resp = "{} for {} more {}".format(resp, daysleft, days)
        else:
            daysuntil = 1 # again, we are counting today
            dt += aday
            while (self.getPom(dt)) not in [0, 4]:
               daysuntil += 1
               dt += aday
            days = " days."
            if daysuntil == 1: days = " day."
            resp = "{}; {} moon in {}{}".format(resp, mp[self.getPom(dt)], daysuntil, days)

        self.respond(replyto, sender, resp)

# spicyCebolla had the idea of randomised greetings so im saving some of her suggestions here in a comment
# <spicyCebolla> "oh no, someone said hi again!"
# <spicyCebolla> "are you saying hi to me? or to a human?"
# <spicyCebolla> "i hope this isn't too forward but i'm glad you said a trigger phrase that i can respond to. welcome i guess!"
# <spicyCebolla> like "you hear someone cursing about refunds" or whatever the usual ones are
    def doHello(self, sender, replyto, msgwords = 0):
        self.msgLog(replyto, "Hello " + sender + ", Welcome to " + CHANNEL)

#    def doRip(self, sender, replyto, msgwords = 0):
#        self.msg(replyto, "rip")

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
        if RE_DICE_CMD.match(msgwords[0]): # !d, !4d is rubbish input.
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
        rolls = []
        tot = 0
        for i in range(0,d0):
            d = random.randrange(1,d1+1)
            rolls.append(str(d))
            tot += d
        if len(rolls) > 1:
            s = "{} = {}".format(" + ".join(rolls), tot)
        else:
            s = str(tot)
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

        # Do not return tnnt if we're not in November.
        chosen_variant = self.variants[random.choice(list(self.variants.keys()))][0][0]
        today_month = datetime.datetime.now().month

        while today_month != 11 and chosen_variant == 'tnnt':  # not November and we got tnnt?
            chosen_variant = self.variants[random.choice(list(self.variants.keys()))][0][0]  # try again

        self.respond(replyto, sender, chosen_variant)

    def doBeer(self, sender, replyto, msgwords):
        self.respond(replyto, sender, random.choice(["It's your shout!", "I thought you'd never ask!",
                                                           "Burrrrp!", "We're not here to f#%k spiders, mate!",
                                                           "One Darwin stubby, coming up!"]))

    def do8ball(self, sender, replyto, msgwords):
        self.respond(replyto, sender, random.choice(["\x1DIt is certain\x0F", "\x1DIt is decidedly so\x0F", "\x1DWithout a doubt\x0F", "\x1DYes definitely\x0F", "\x1DYou may rely on it\x0F",
                                                           "\x1DAs I see it, yes\x0F", "\x1DMost likely\x0F", "\x1DOutlook good\x0F", "\x1DYes\x0F", "\x1DSigns point to yes\x0F", "\x1DReply hazy try again\x0F",
                                                           "\x1DAsk again later\x0F", "\x1DBetter not tell you now\x0F", "\x1DCannot predict now\x0F", "\x1DConcentrate and ask again\x0F",
                                                           "\x1DDon't count on it\x0F", "\x1DMy reply is no\x0F", "\x1DMy sources say no\x0F", "\x1DOutlook not so good\x0F", "\x1DVery doubtful\x0F"]))

    def doStatus(self, sender, replyto, msgwords):
        if sender not in self.admin:
            self.respond(replyto, sender, "Admin access required.")
            return

        # Get memory usage of current process
        try:
            import resource
            mem_usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # On Linux, ru_maxrss is in KB
            mem_mb = mem_usage / 1024
        except ImportError:
            mem_mb = "N/A"

        # Calculate uptime
        uptime_seconds = int(time.time() - self.starttime)
        uptime_days = uptime_seconds // 86400
        uptime_hours = (uptime_seconds % 86400) // 3600
        uptime_mins = (uptime_seconds % 3600) // 60

        # Count active file monitors
        monitor_count = 0
        for v in self.xlogfiles:
            monitor_count += len(self.xlogfiles[v])
        for v in self.livelogs:
            monitor_count += len(self.livelogs[v])

        # Count queries in queue
        query_count = len(self.queries) if hasattr(self, 'queries') else 0

        # Count cached messages
        msg_count = len(self.tellbuf) if hasattr(self, 'tellbuf') else 0

        # Count rate limited users
        rate_limit_count = len(self.rate_limits) if hasattr(self, 'rate_limits') else 0

        # Count users under abuse penalty
        abuse_penalty_count = len(self.abuse_penalties) if hasattr(self, 'abuse_penalties') else 0

        # Build status message
        status_parts = []
        status_parts.append("Status: {} on {}".format(NICK, SERVERTAG))
        status_parts.append("Uptime: {}d {}h {}m".format(uptime_days, uptime_hours, uptime_mins))
        if mem_mb != "N/A":
            status_parts.append("Memory: {:.1f}MB".format(mem_mb))
        status_parts.append("Monitors: {}".format(monitor_count))
        status_parts.append("Queries: {}".format(query_count))
        status_parts.append("Messages: {}".format(msg_count))
        status_parts.append("RateLimit: {}".format(rate_limit_count))
        if abuse_penalty_count > 0:
            status_parts.append("AbusePenalty: {}".format(abuse_penalty_count))

        self.respond(replyto, sender, " | ".join(status_parts))

    # The following started as !tea resulting in the bot making a cup of tea.
    # Now it does other stuff.
    bev = { "serves": ["delivers", "tosses", "passes", "pours", "hands", "throws", "zaps", "flings", "hurls", "lobs", "beams up", "gifts", "slides"],
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
        for vchoice in range(MAX_VARIANT_CHOICES):
            vessel = random.choice(self.bev["vessel"]["all"])
            if drink not in self.bev["vessel"]: break # anything goes for these
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

    # Cache for saving rumors files so it doesn't need to redownload them all the time.
    # Data structure is { url: (timestamp, ["rumor1", "rumor2", ...]) }
    rumorCache = {}

    # Helper for accessing the cache.
    # Entries are considered out of date if more than an hour old and will be redownloaded.
    # Return rumors list if successful, False if some error.
    def rumorCacheGet(self, url):
        now = time.time()
        if not url in self.rumorCache or now > self.rumorCache[url][0] + 3600:
            print("url", url, "not found or expired in rumor cache, downloading...")
            try:
                r = requests.get(url, timeout=10)
                if r.status_code != requests.codes.ok:
                    print(f"Failed to fetch {url}: HTTP {r.status_code}")
                    return False

                # filter out comments (# at start of line) and blanks, no point saving them
                rumors = [r for r in filter(lambda r : len(r) > 0 and r[0] != '#', r.text.splitlines())]
                self.rumorCache[url] = (now, rumors)
            except requests.exceptions.Timeout:
                print(f"Timeout fetching {url}")
                return False
            except requests.exceptions.ConnectionError as e:
                print(f"Connection error fetching {url}: {e}")
                return False
            except requests.exceptions.RequestException as e:
                print(f"Error fetching {url}: {e}")
                return False

        return self.rumorCache[url][1]

    def doRumor(self, sender, replyto, msgwords):
        '''
        !rumor                                         => random rumor from vanilla
        !rumor variant                                 => random rumor from that variant
        !rumor [variant] true|false                    => random rumor that will come only from rumors.tru/.fal
        !rumor [variant] [true|false] arbitrary-string => random rumor matching arbitrary-string
        ... though the order of arguments is more flexible than this.
        '''
        suffix = None
        variant = None
        match = None
        getBoth = False
        for w in msgwords[1:]: # msgwords[0] is "rumor" from the command
            if suffix is None and w == 'true':
                suffix = 'tru'
            elif suffix is None and w == 'false':
                suffix = 'fal'
            else:
                var = self.varalias(w)
                if variant is None and var in self.variants:
                    variant = var
                else:
                    # not some other argument, assume string match; combine
                    # strings for multiple words
                    if match is not None:
                        match += ' ' + w
                    else:
                        match = w

        # defaults if unspecified
        if variant is None:
            variant = 'nh370'
        if suffix is None:
            if match is None:
                suffix = random.choice(['tru','fal'])
            else:
                # if no t/f is specified but a string match is, then we need to
                # get both rumor files. force to true here so we can do a
                # s/tru/fal/ later
                suffix = 'tru'
                getBoth = True

        if variant == 'nh13d':
            # 1.3d is a special snowflake that doesn't have separate files for
            # true and false and also doesn't have a dat/ dir.
            suffix = 'base'
            url = "https://raw.githubusercontent.com/bhaak/nethack-save-xml/067c3ccc/rumors.base"
            getBoth = False
        elif len(self.variants[variant]) < 4 or self.variants[variant][3] is None:
            self.msgLog(replyto, "I don't have any rumors for " + variant + ".")
            return
        else:
            url = 'https://raw.githubusercontent.com/' + self.variants[variant][3] + '/dat/rumors.' + suffix

        rumors = self.rumorCacheGet(url)
        if rumors == False:
            self.msgLog(replyto, "Sorry, I couldn't get the rumors file.")
            return
        if getBoth:
            url = url[:-3] + 'fal' # url was forced to 'tru' earlier...
            moreRumors = self.rumorCacheGet(url)
            if moreRumors == False:
                self.msgLog(replyto, "Sorry, I couldn't get the rumors file.")
                return
            rumors += moreRumors

        # Simple (case insensitive) string match; this could be a regex match
        # but that's probably overkill
        if match is not None:
            rumors = [r for r in filter(lambda r : match.lower() in r.lower(), rumors)]

        # potential future improvement: grab and cache a copy of the vanilla
        # rumors, and bias against picking one of those if a variant is specified

        if len(rumors) == 0:
            self.msgLog(replyto, 'No rumors matching "' + match + '".')
            return

        self.msgLog(replyto, random.choice(rumors))


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
        rcpt_lower = rcpt.lower()
        messages = self.tellbuf.get(rcpt_lower, [])

        # Prevent memory leaks by limiting total tell messages
        total_messages = sum(len(msgs) for msgs in self.tellbuf.values())
        if total_messages >= MAX_TELLBUF_MESSAGES:
            self.respond(replyto, sender, "Tell message limit reached, try again later")
            return

        messages.append((forwardto,sender,time.time(),message))
        self.tellbuf[rcpt_lower] = messages
        self.tellbuf.sync()
        # Sanitize sender and recipient names to prevent format string injection
        safe_sender = sanitize_format_string(sender)
        safe_rcpt = sanitize_format_string(rcpt)
        self.msgLog(replyto,random.choice(willDo).format(safe_sender,safe_rcpt))

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
                    if sender not in nicksfrom: nicksfrom.append(sender)
                self.respond(user,user, "Message from " + sender + " at " + self.msgTime(ts) + ": " + message)
            # "tom" "tom and dick" "tom, dick, and harry"
            if nicksfrom:
                # Sanitize all nicknames to prevent format string injection
                safe_nicks = [sanitize_format_string(nick) for nick in nicksfrom]
                if len(safe_nicks) == 1:
                    fromstr = safe_nicks[0]
                elif len(safe_nicks) == 2:
                    fromstr = "{} and {}".format(safe_nicks[0], safe_nicks[1])
                else:
                    # oxford comma for 3 or more
                    fromstr = "{}, and {}".format(", ".join(safe_nicks[:-1]), safe_nicks[-1])
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

        # Prevent memory leaks by limiting concurrent queries
        if len(self.queries) >= MAX_QUERIES:
            self.respond(replyto, sender, "Query limit reached, try again later")
            return

        q = self.newQueryId()
        self.queries[q] = {}
        self.queries[q]["callback"] = callback
        self.queries[q]["replyto"] = replyto
        self.queries[q]["sender"] = sender
        self.queries[q]["resp"] = {}
        self.queries[q]["timestamp"] = time.time()
        message = "#Q# " + " ".join([q,sender] + msgwords)

        for sl in self.slaves:
            print("forwardQuery: " + sl)
            self.msg(sl,message)
        # set up the timeout in 5 seconds.
        reactor.callLater(QUERY_TIMEOUT, self.timeoutQuery, q)

    # Multi-server command entry point (forwards query to slaves)
    def multiServerCmd(self, sender, replyto, msgwords):
        # NOTE: Burst protection and rate limiting already handled in main dispatch
        command = msgwords[0].lower()
        with open("/tmp/debug.log", "a") as f:
            f.write(f"DEBUG: multiServerCmd called with command: {command}, sender: {sender}\n")
            f.flush()

        if msgwords[0] in self.checkUsage:
            with open("/tmp/debug.log", "a") as f:
                f.write(f"DEBUG: Checking usage for {msgwords[0]}\n")
                f.flush()
            if not self.checkUsage[msgwords[0]](sender, replyto, msgwords):
                with open("/tmp/debug.log", "a") as f:
                    f.write(f"DEBUG: Usage check failed for {msgwords[0]}\n")
                    f.flush()
                return

        with open("/tmp/debug.log", "a") as f:
            f.write(f"DEBUG: self.slaves = {getattr(self, 'slaves', 'NOT_SET')}\n")
            f.write(f"DEBUG: TEST = {TEST}\n")
            f.flush()

        if self.slaves:
            with open("/tmp/debug.log", "a") as f:
                f.write(f"DEBUG: Forwarding query to slaves\n")
                f.flush()
            self.forwardQuery(sender, replyto, msgwords, self.callBacks.get(msgwords[0],None))
        else:
            # For testing: provide a simple response when no slaves configured
            if TEST:
                with open("/tmp/debug.log", "a") as f:
                    f.write(f"DEBUG: Sending test response for {command}\n")
                    f.flush()
                self.respond(replyto, sender, f"Test response for !{command} (no slaves configured)")
            else:
                with open("/tmp/debug.log", "a") as f:
                    f.write(f"DEBUG: No slaves and not in TEST mode\n")
                    f.flush()

    # !players - respond to forwarded query and actually pull the info
    def getPlayers(self, master, sender, query, msgwords):
        plrvar_list = []
        # Build a list of all ttyrec files with their associated variant
        for var in self.inprog:
            for inpdir in self.inprog[var]:
                # Get all ttyrec files in this directory at once
                ttyrec_files = glob.glob(inpdir + "*.ttyrec")
                for inpfile in ttyrec_files:
                    # /stuff/crap/PLAYER:shit:garbage.ttyrec
                    # we want AFTER last '/', BEFORE 1st ':'
                    player = inpfile.split("/")[-1].split(":")[0]
                    plrvar_list.append(player + " " + self.displaytag(var))
        if not plrvar_list:
            plrvar = "No current players"
        else:
            plrvar = " ".join(plrvar_list) + " "
        response = "#R# " + query + " " + self.displaytag(SERVERTAG) + " " + plrvar
        self.msg(master, response)

    # !players callback. Actually print the output.
    def outPlayers(self,q):
        outmsg = " :: ".join(q["resp"].values())
        self.respond(q["replyto"],q["sender"],outmsg)

    def usageWhereIs(self, sender, replyto, msgwords):
        if (len(msgwords) != 2):
            self.respond(replyto, sender, "!" + msgwords[0] + " <player> - finds a player in the dungeon.")
            return False
        return True

    def getWhereIs(self, master, sender, query, msgwords):
        ammy = ["", " (with Amulet)"]

        # Validate player name to prevent path traversal
        player_name = msgwords[1]
        if "/" in player_name or ".." in player_name or "\\" in player_name:
            self.msg(master, "#R# " + query + " " + self.displaytag(SERVERTAG)
                     + " Invalid player name.")
            return

        target_player = player_name.lower()
        # look for inrpogress file first, only report active games
        for var in self.inprog:
            # Check if player has an active game in this variant
            player_found = False
            for inpdir in self.inprog[var]:
                ttyrec_pattern = "{}{}:*.ttyrec".format(inpdir, player_name)
                ttyrec_files = glob.glob(ttyrec_pattern)
                if ttyrec_files:
                    player_found = True
                    break

            if player_found:
                # Look for whereis file
                for widir in self.whereis[var]:
                    whereis_file = "{}{}.whereis".format(widir, player_name)
                    # Try case-insensitive match
                    whereis_files = glob.glob(whereis_file)
                    if not whereis_files:
                        # Try with different case
                        whereis_pattern = "{}*.whereis".format(widir)
                        for wipath in glob.glob(whereis_pattern):
                            if wipath.split("/")[-1].lower() == (player_name + ".whereis").lower():
                                whereis_files = [wipath]
                                break

                    if whereis_files:
                        wipath = whereis_files[0]
                        plr = wipath.split("/")[-1].split(".")[0] # Correct case
                        with open(wipath, "rb") as f:
                            wirec = parse_xlogfile_line(f.read(),":")

                        self.msg(master, "#R# " + query
                                 + " " + self.displaytag(SERVERTAG) + " " + plr
                                 + " "+self.displaytag(var)
                                 + ": ({role} {race} {gender} {align}) T:{turns} ".format(**wirec)
                                 + self.dungeons[var][wirec["dnum"]]
                                 + " level: " + str(wirec["depth"])
                                 + ammy[wirec["amulet"]])
                        return
        self.msg(master, "#R# " + query + " " + self.displaytag(SERVERTAG)
                                        + " " + player_name
                                        + " is not currently playing on this server.")

    def outWhereIs(self,q):
        player = ''
        msgs = []
        for server in q["resp"]:
            if " is not currently playing" in q["resp"][server]:
                player = q["resp"][server].split(" ")[1]
            else:
                msgs += [q["resp"][server]]
        outmsg = " :: ".join(msgs)
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
            if vp in self.variants:
                # !streak dnh Tangles
                return (msgwords[2], vp)
            if pv in self.variants:
                # !streak K2 UnNethHack
                return (msgwords[1],pv)
            # !streak bogus garbage
            if not SLAVE: self.respond(replyto,sender,"Usage: !" +msgwords[0] +" [variant] [player]")
            return (None, None)
        if len(msgwords) == 2:
            vp = self.varalias(msgwords[1])
            if vp in self.variants:
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
            stats_parts = []

            # Roles
            role_stats = []
            for role in self.variants[var][1]:
                role = role.title() # capitalise the first letter
                if role in self.asc[var][plr]:
                    totasc += self.asc[var][plr][role]
                    role_stats.append("{}x{}".format(self.asc[var][plr][role], role))
            if role_stats:
                stats_parts.append(" ".join(role_stats))

            # Races
            race_stats = []
            for race in self.variants[var][2]:
                race = race.title()
                if race in self.asc[var][plr]:
                    race_stats.append("{}x{}".format(self.asc[var][plr][race], race))
            if race_stats:
                stats_parts.append(" ".join(race_stats))

            # Alignments
            align_stats = []
            for alig in self.aligns:
                if alig in self.asc[var][plr]:
                    align_stats.append("{}x{}".format(self.asc[var][plr][alig], alig))
            if align_stats:
                stats_parts.append(" ".join(align_stats))

            # Genders
            gender_stats = []
            for gend in self.genders:
                if gend in self.asc[var][plr]:
                    gender_stats.append("{}x{}".format(self.asc[var][plr][gend], gend))
            if gender_stats:
                stats_parts.append(" ".join(gender_stats))

            stats = " " + ", ".join(stats_parts) + "."
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
        variant_stats = []
        for var in self.asc:
            totgames += self.allgames[var].get(plr,0)
            if plr in self.asc[var]:
                varasc = self.asc[var][plr].get("Mal",0)
                varasc += self.asc[var][plr].get("Fem",0)
                varasc += self.asc[var][plr].get("Nbn",0)
                totasc += varasc
                variant_stats.append("{}: {} ({:0.2f}%)".format(
                    self.displaystring[var], varasc,
                    (100.0 * varasc) / self.allgames[var][plr]))
        if totasc:
            stats = ", ".join(variant_stats)
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
        outmsg = " :: ".join(msgs)
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
            reply = "{} {} {} Max: {} ({} - {})".format(
                reply, self.displaytag(SERVERTAG), PLR + self.displaytag(var),
                llength, self.streakDate(lstart), self.streakDate(lend))
            if clength > 0:
                if cstart == lstart:
                    reply += "(current)"
                else:
                    reply = "{}. Current: {} (since {})".format(
                        reply, clength, self.streakDate(cstart))
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
        reply = "{} {} {} Max[{}]: {} ({} - {})".format(
            reply, self.displaytag(SERVERTAG), PLR, self.displaystring[lvar],
            lmax, self.streakDate(lsmax), self.streakDate(lemax))
        if cmax > 0:
            if csmax == lsmax:
                reply += "(current)"
            else:
                reply = "{}. Current[{}]: {} (since {})".format(
                    reply, self.displaystring[cvar], cmax, self.streakDate(csmax))
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
            if RE_DIGITS.match(msgwords[1]):
                self.plr_tc[sender.lower()] = int(msgwords[1])
                self.plr_tc.sync()
                self.msg(master, "#R# " + query + " " + self.displaytag(SERVERTAG)
                                 + " Min reported turncount for " + sender.lower()
                                 + " set to " + msgwords[1])
                return
        if len(msgwords) == 1:
            if sender.lower() in self.plr_tc:
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
                if RE_DIGITS.match(msgwords[2]):
                    self.plr_tc[msgwords[1].lower()] = int(msgwords[2])
                    self.plr_tc.sync()
                    self.msg(master, "#R# " + query + " " + self.displaytag(SERVERTAG)
                                     + " Min reported turncount for " + msgwords[1].lower()
                                     + " set to " + msgwords[2])
                    return
            if len(msgwords) == 2:
                if msgwords[1].lower() in self.plr_tc:
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
            if (sender == DCBRIDGE):
                message = message.partition("<")[2] #everything after the first <
                sender,x,message = message.partition(">") #everything remaining before/after the first >
                message = RE_SPACE_COLOR.sub('', message) # everything after the first space and any colour codes
                if len(sender) == 0: return
        else: #private msg
            replyto = sender
        # Hello processing first.
        if RE_HELLO.match(message):
            self.doHello(sender, replyto)
#        if re.match(r'^(rip|r\.i\.p|rest in p).*$', message.lower()):
#            self.doRip(sender, replyto)
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
        if RE_DICE_FULL.match(msgwords[0]):
            self.rollDice(sender, replyto, msgwords)
            return
        if self.commands.get(msgwords[0].lower(), False):
            command = msgwords[0].lower()
            with open("/tmp/debug.log", "a") as f:
                f.write(f"DEBUG: Main dispatch found command: {command}\n")
                f.flush()

            # Internal bot commands (#q#, #r#) bypass all rate limiting
            if command.startswith('#') and command.endswith('#'):
                with open("/tmp/debug.log", "a") as f:
                    f.write(f"DEBUG: Internal command {command} bypassing rate limits\n")
                    f.flush()
                self.commands[command](sender, replyto, msgwords)
                return

            # Apply burst protection to user commands only
            if not self._checkBurstProtection(sender, command):
                with open("/tmp/debug.log", "a") as f:
                    f.write(f"DEBUG: Main dispatch blocked {command} due to burst protection\n")
                    f.flush()
                return  # Silently ignore burst commands

            # Apply rate limiting to user commands only
            if not self._checkRateLimit(sender, command):
                with open("/tmp/debug.log", "a") as f:
                    f.write(f"DEBUG: Main dispatch blocked {command} due to rate limiting\n")
                    f.flush()
                # Check if we should send a penalty message (prevent penalty spam)
                if not self._shouldSendPenaltyMessage(sender):
                    return  # Silently ignore to prevent penalty message spam

                # Provide specific error message based on penalty type
                if hasattr(self, 'abuse_penalties') and sender in self.abuse_penalties:
                    remaining = int(self.abuse_penalties[sender] - time.time())
                    self.respond(replyto, sender, f"Abuse penalty active: {remaining//60}m {remaining%60}s remaining. (Triggered by spamming consecutive commands)")
                else:
                    self.respond(replyto, sender, f"Rate limit exceeded. Please wait before using !{command} again.")
                return

            with open("/tmp/debug.log", "a") as f:
                f.write(f"DEBUG: Main dispatch calling {command} -> {self.commands[command]}\n")
                f.flush()
            self.commands[command](sender, replyto, msgwords)
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
        return (name.lower() in self.plr_tc
           and turns < self.plr_tc[name.lower()])

    def generate_dumplog_url(self, game, dumpfile):
        """Generate dumplog URL, checking local storage first, then S3.

        Returns the URL if file exists in either location, None otherwise.
        """
        # First check if file exists locally
        if os.path.exists(dumpfile):
            # File exists locally, use regular URL
            dumpurl = urllib.parse.quote(game["dumpfmt"].format(**game))
            return self.dump_url_prefix.format(**game) + dumpurl

        # File doesn't exist locally - generate S3 URL
        # S3 URL structure differs by server
        s3_base = None
        if SERVERTAG == "hdf-us":
            s3_base = "https://hdf-us.s3.amazonaws.com/dumplogs/"
        elif SERVERTAG == "hdf-eu":
            s3_base = "https://hdf-eu.s3.amazonaws.com/dumplogs/"
        elif SERVERTAG == "hdf-au":
            s3_base = "https://hdf-au.s3.amazonaws.com/dumplogs/"

        if s3_base:
            # Generate S3 URL
            dumppath = urllib.parse.quote(game["dumpfmt"].format(**game))
            # S3 path structure: dumplogs/{name[0]}/{name}/{variant}/dumplog/{filename}
            s3_url = s3_base + "{name[0]}/{name}/".format(**game) + dumppath
            return s3_url

        # If we can't determine S3 location, return None
        return None

    def xlogfileReport(self, game, report = True):
        var = game["variant"] # Make code less ugly
        # lowercased name is used for lookups
        lname = game["name"].lower()
        # "allgames" for a player even counts scummed games
        if not lname in self.allgames[var]:
            self.allgames[var][lname] = 0
        self.allgames[var][lname] += 1

        dumplog = game.get("dumplog",False)
        if dumplog and var != "dyn":
            game["dumplog"] = fixdump(dumplog)
        # Need to figure out the dump path before messing with the name below
        dumpfile = (self.dump_file_prefix + game["dumpfmt"]).format(**game)

        # Generate dumplog URL using new method that checks both local and S3
        if TEST:
            # In test mode, always generate a URL
            dumpurl = urllib.parse.quote(game["dumpfmt"].format(**game))
            dumpurl = self.dump_url_prefix.format(**game) + dumpurl
        else:
            # In production, check both local and S3 locations
            generated_url = self.generate_dumplog_url(game, dumpfile)
            if generated_url:
                dumpurl = generated_url
            else:
                dumpurl = "(sorry, no dump exists for {variant}:{name})".format(**game)
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
            if self.plr_tc_notreached(game["name"], game["turns"]): report = False # ignore due to !setmintc, only if not ascended

        if self.startscummed(game): return
        # only populate "!lastgame" fields for non-scummed games
        self.lg["{variant}:{name}".format(**game).lower()] = dumpurl
        if (game["endtime"] > self.lge.get(lname, 0)):
            self.lge[lname] = game["endtime"]
            self.lg[lname] = dumpurl
        self.lg[var] = dumpurl
        if (game["endtime"] > self.tlastgame):
            self.lastgame = dumpurl
            self.tlastgame = game["endtime"]

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
                game["duration_str"] = f"wc[{game['wallclock']}]"

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
        # nh370 livelog uses name instead of player
        if "name" in event and "player" not in event:
            event["player"] = event["name"]
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
                       "{message}, on T:{turns} ({realtime_fmt})").format(**event)
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
        # Clean up shelve databases
        if hasattr(self, 'tellbuf') and self.tellbuf is not None:
            self.tellbuf.close()
        if hasattr(self, 'plr_tc') and self.plr_tc is not None:
            self.plr_tc.close()

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
    #log.startLogging(DailyLogFile.fromFullPath(LOGBASE))

    # create factory protocol and application
    f = DeathBotFactory()

    # connect factory to this host and port
    # Use plain TCP for testing (no SSL)
    reactor.connectTCP(HOST, PORT, f)

    # run bot
    reactor.run()
