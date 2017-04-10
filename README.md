# beholder
IRC Announce Bot for hardfought.org, based on http://ascension.run/deathbot.py
run with twistd, as follows:
 twistd -y beholder.py
Some enhancements to the original deathbot code include:
 - delimiter-agnostic xlogfile parsing (because some newer variants have moved
   from the traditional ':' delimiter to a <tab> character.
 - Dumplog url announcements for ascended games.
 - Various commands (inspired by #nethack's Rodney), as follows:
    !time - report local time of the server.
    !ping - determine if the bot is alive
    !lastgame [variant] [player] - report dumplog url of most recent game.
    !lastasc [variant] [player] - as above, but ascended games only.
    !tell <nick> <message> - repeat <message> next time <nick> is active.
    !beer, !goat - undocumented :P
