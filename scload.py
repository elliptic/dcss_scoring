import MySQLdb
import re
import os
import os.path
import crawl_utils

import logging
from logging import debug, info, warn, error

import ConfigParser
import imp
import sys
import optparse

oparser = optparse.OptionParser()
oparser.add_option('-n', '--no-load', action='store_true', dest='no_load')
OPT, ARGS = oparser.parse_args()

# Limit rows read to so many for testing.
LIMIT_ROWS = 0

# Start and end of the tournament, UTC.
START_TIME = '20090801'
END_TIME   = '20090901'

OLDEST_VERSION = '0.4'

CDO = 'http://crawl.develz.org/'

# Log and milestone files. A tuple indicates a remote file with t[1]
# being the URL to wget -c from. Files can be in any order, loglines
# will be read in strict chronological order.

LOGS = [ 'cao-logfile-0.4',
         'cao-logfile-0.5',
         ('cdo-logfile-0.4', CDO + 'allgames-0.4.txt'),
         ('cdo-logfile-0.5', CDO + 'allgames-0.5.txt')
       ]

MILESTONES = [ 'cao-milestones-0.5',
               'cao-milestones-0.4',
               ('cdo-milestones-0.4', CDO + 'milestones-0.4.txt'),
               ('cdo-milestones-0.5', CDO + 'milestones-0.5.txt')
               ]

BLACKLIST_FILE = 'blacklist.txt'
EXTENSION_FILE = 'modules.ext'
SCORING_DB = 'scoring'
COMMIT_INTERVAL = 3000
CRAWLRC_DIRECTORY = '/home/crawl/chroot/dgldir/rcfiles/'

LISTENERS = [ ]
TIMERS = [ ]

class Blacklist(object):
  def __init__(self, filename):
    self.filename = filename
    if os.path.exists(filename):
      info("Loading blacklist from " + filename)
      self.load_blacklist()

  def load_blacklist(self):
    fh = open(self.filename)
    lines = fh.readlines()
    fh.close()
    self.blacklist = [apply_dbtypes(parse_logline(x.strip()))
                      for x in lines if x.strip()]

  def is_blacklisted(self, game):
    for b in self.blacklist:
      if xlog_match(b, game):
        return True
    return False

class CrawlEventListener(object):
  """The way this is intended to work is that on receipt of an event
  ... we shoot the messenger. :P"""
  def initialize(self, db):
    """Called before any processing, do your initialization here."""
    pass
  def cleanup(self, db):
    """Called after we're done processing, do cleanup here."""
    pass
  def logfile_event(self, cursor, logdict):
    """Called for each logfile record. cursor will be in a transaction."""
    pass
  def milestone_event(self, cursor, mdict):
    """Called for each milestone record. cursor will be in a transaction."""
    pass

class CrawlCleanupListener (CrawlEventListener):
  def __init__(self, fn):
    self.fn = fn

  def cleanup(self, db):
    c = db.cursor()
    try:
      self.fn(c)
    finally:
      c.close()

class CrawlTimerListener:
  def __init__(self, fn=None):
    self.fn = fn

  def run(self, cursor, elapsed_time):
    if self.fn:
      self.fn(cursor)

class CrawlTimerState:
  def __init__(self, interval, listener):
    self.listener = listener
    self.interval = interval
    # Fire the first event immediately.
    self.target   = 0

  def run(self, cursor, elapsed):
    if self.target <= elapsed:
      self.listener.run(cursor, elapsed)
      self.target = elapsed + self.interval

#########################################################################
# xlogfile classes. xlogfiles are a colon-separated-field,
# newline-terminated-record key=val format. Colons in values are
# escaped by doubling. Originally created by Eidolos for NetHack logs
# on n.a.o, and adopted by Crawl as well.

# These classes merely read lines from the logfile, and do not parse them.

class Xlogline:
  """A dictionary from an Xlogfile, along with information about where and
  when it came from."""
  def __init__(self, owner, filename, offset, time, xdict, processor):
    self.owner = owner
    self.filename = filename
    self.offset = offset
    self.time = time
    if not time:
      raise Exception, \
          "Xlogline time missing from %s:%d: %s" % (filename, offset, xdict)
    self.xdict = xdict
    self.processor = processor

  def __cmp__(self, other):
    ltime = self.time
    rtime = other.time
    # Descending time sort order, so that later dates go first.
    if ltime > rtime:
      return -1
    elif ltime < rtime:
      return 1
    else:
      return 0

  def process(self, cursor):
    self.processor(cursor, self.filename, self.offset, self.xdict)

class Xlogfile:
  def __init__(self, filename, proc_op, blacklist=None):
    if isinstance(filename, tuple):
      self.local = False
      self.filename = filename[0]
      self.url = filename[1]
    else:
      self.local = True
      self.filename = filename
    self.handle = None
    self.offset = None
    self.proc_op = proc_op
    self.size  = None
    self.blacklist = blacklist

  def reinit(self):
    """Reinitialize for a further read from this file."""
    # If this is a local file, take a snapshot of the file size here.
    # We will not read past this point. This is important because local
    # files grow constantly, whereas remote files grow only when we pull
    # them from the remote server, so we should not read past the point
    # in the local file corresponding to the point where we pulled from the
    # remote server.
    if self.local:
      self.size = os.path.getsize(self.filename)
    else:
      self.fetch_remote()

  def fetch_remote(self):
    info("Fetching remote %s to %s with wget -c" % (self.url, self.filename))
    res = os.system("wget -q -c %s -O %s" % (self.url, self.filename))
    if res != 0:
      raise IOError, "Failed to fetch %s with wget" % self.url

  def _open(self):
    try:
      self.handle = open(self.filename)
    except:
      warn("Cannot open %s" % self.filename)
      pass

  def have_handle(self):
    if self.handle:
      return True
    self._open()
    return self.handle

  def line(self, cursor):
    if not self.have_handle():
      return

    while True:
      if not self.offset:
        xlog_seek(self.filename, self.handle,
                  dbfile_offset(cursor, self.filename))
        self.offset = self.handle.tell()

      # Don't read beyond the last snapshot size for local files.
      if self.local and self.offset >= self.size:
        return None

      line = self.handle.readline()
      newoffset = self.handle.tell()
      if not line or not line.endswith("\n") or \
            (self.local and newoffset > self.size):
        # Reset to last read
        self.handle.seek(self.offset)
        return None

      self.offset = newoffset
      # If this is a blank line, advance the offset and keep reading.
      if not line.strip():
        continue

      d = xlog_dict(line)
      xdict = apply_dbtypes(d)
      if self.blacklist and self.blacklist.is_blacklisted(xdict):
        # Blacklisted games are mauled here:
        xdict['ktyp'] = 'blacklist'
        xdict['place'] = 'D:1'
        xdict['xl'] = 1
        xdict['lvl'] = 1
        xdict['tmsg'] = 'was blacklisted.'
        xdict['vmsg'] = 'was blacklisted.'

      xdict['source_file'] = self.filename
      xline = Xlogline( self, self.filename, self.offset,
                        xdict.get('end') or xdict.get('time'),
                        xdict, self.proc_op )
      return xline

class Logfile (Xlogfile):
  def __init__(self, filename, blacklist):
    Xlogfile.__init__(self, filename, process_log, blacklist)

class MilestoneFile (Xlogfile):
  def __init__(self, filename):
    Xlogfile.__init__(self, filename, process_milestone)

class MasterXlogReader:
  """Given a list of Xlogfile objects, calls the process operation on the oldest
  line from all the logfiles, and keeps doing this until all lines have been
  processed in chronological order."""
  def __init__(self, xlogs):
    self.xlogs = xlogs

  def reinit(self):
    for x in self.xlogs:
      x.reinit()

  def tail_all(self, cursor):
    self.reinit()
    lines = [ line for line in [ x.line(cursor) for x in self.xlogs ]
              if line ]

    proc = 0
    while lines:
      # Sort dates in descending order.
      lines.sort()
      # And pick the oldest.
      oldest = lines.pop()
      # Grab a replacement for the one we're going to read from the same file:
      newline = oldest.owner.line(cursor)
      if newline:
        lines.append(newline)
      # And process the line
      oldest.process(cursor)
      proc += 1
      if LIMIT_ROWS > 0 and proc >= LIMIT_ROWS:
        break
      if proc % 3000 == 0:
        info("Processed %d lines." % proc)
    if proc > 0:
      info("Done processing %d lines." % proc)

def connect_db():
  connection = MySQLdb.connect(host='localhost',
                               user='scoring',
                               db=SCORING_DB)
  return connection

def parse_logline(logline):
  """This function takes a logfile line, which is mostly separated by colons,
  and parses it into a dictionary (which everyone except Python calls a hash).
  Because the Crawl developers are insane, a double-colon is an escaped colon,
  and so we have to be careful not to split the logfile on locations like
  D:7 and such. It also works on milestones and whereis."""
  # This is taken from Henzell. Yay Henzell!
  if not logline:
    raise Exception, "no logline"
  if logline[0] == ':' or (logline[-1] == ':' and not logline[-2] == ':'):
    raise Exception,  "starts with colon"
  if '\n' in logline:
    raise Exception, "more than one line"
  logline = logline.replace("::", "\n")
  details = dict([(item[:item.index('=')], item[item.index('=') + 1:])
                  for item in logline.split(':')])
  for key in details:
    details[key] = details[key].replace("\n", ":")
  return details

def xlog_set_killer_group(d):
  killer = d.get('killer')
  if not killer:
    ktyp = d.get('ktyp')
    if ktyp:
      d['ckiller'] = ktyp
    return

  m = R_GHOST_NAME.search(killer)
  if m:
    d['ckiller'] = 'player ghost'
    return

  m = R_HYDRA.search(killer)
  if m:
    d['ckiller'] = m.group(1)
    return
  killer = R_ARTICLE.sub('', killer)
  d['ckiller'] = killer

def xlog_milestone_fixup(d):
  for field in [x for x in ['lv', 'uid'] if d.has_key(x)]:
    del d[field]
  verb = d['type']
  milestone = d['milestone']
  noun = None

  if verb == 'unique':
    verb = 'uniq'

  if verb == 'uniq':
    match = R_MILE_UNIQ.findall(milestone)
    if match[0][0] == 'banished':
      verb = 'uniq.ban'
    noun = match[0][1]
  if verb == 'ghost':
    match = R_MILE_GHOST.findall(milestone)
    if match[0][0] == 'banished':
      verb = 'ghost.ban'
    noun = match[0][1]
  if verb == 'rune':
    noun = R_RUNE.findall(milestone)[0]
  if verb == 'god.worship':
    noun = R_GOD_WORSHIP.findall(milestone)[0]
  elif verb == 'god.renounce':
    noun = R_GOD_RENOUNCE.findall(milestone)[0]
  elif verb == 'god.mollify':
    noun = R_GOD_MOLLIFY.findall(milestone)[0]
  noun = noun or milestone
  d['verb'] = verb
  d['noun'] = noun

def xlog_match(ref, target):
  """Returns True if all keys in the given reference dictionary are
associated with the same values in the target dictionary."""
  for key in ref.keys():
    if ref[key] != target.get(key):
      return False
  return True

def canonical_killer(g):
  raw = g.get('killer') or g.get('ktyp')

def xlog_dict(logline):
  d = parse_logline(logline.strip())

  # Fake a raceabbr field to group on race without failing on
  # draconians.
  if d.get('char'):
    d['raceabbr'] = d['char'][0:2]
    d['clsabbr'] = d['char'][2:]

  d['crace'] = d['race']
  if d['race'].find('Draconian') != -1:
    d['crace'] = 'Draconian'

  if d.get('tmsg') and not d.get('vmsg'):
    d['vmsg'] = d['tmsg']

  if not d.get('nrune') and not d.get('urune'):
    d['nrune'] = 0
    d['urune'] = 0

  # Fixup rune madness where one or the other is set, but not both.
  if d.get('nrune') is not None or d.get('urune') is not None:
    d['nrune'] = d.get('nrune') or d.get('urune')
    d['urune'] = d.get('urune') or d.get('nrune')

  if d.has_key('milestone'):
    xlog_milestone_fixup(d)
  xlog_set_killer_group(d)

  return d

# The mappings in order so that we can generate our db queries with all the
# fields in order and generally debug things more easily.

# Note: all fields must be present here, even if their names are the
# same in logfile and db.
RAW_LOG_DB_MAPPINGS = [
  'source_file',
  'v',
  'lv',
  'name',
  'uid',
  'race',
  'crace',
  'raceabbr',
  'clsabbr',
  'cls',
  [ 'char', 'charabbr' ],
  'xl',
  'sk',
  'sklev',
  'title',
  'place',
  'br',
  'lvl',
  'ltyp',
  'hp',
  'mhp',
  'mmhp',
  [ 'str', 'strength' ],
  [ 'int', 'intelligence' ],
  [ 'dex', 'dexterity' ],
  'god',
  [ 'start', 'start_time' ],
  'dur',
  'turn',
  'sc',
  'ktyp',
  'killer',
  'ckiller',
  'dam',
  'piety',
  'pen',
  [ 'end', 'end_time' ],
  'tmsg',
  'vmsg',
  'kaux',
  'kills',
  'nrune',
  'urune',
  'gold',
  'goldfound',
  'goldspent'
  ]

LOG_DB_MAPPINGS = [isinstance(x, str) and [x, x] or x
                   for x in RAW_LOG_DB_MAPPINGS]

LOG_DB_COLUMNS = [x[1] for x in LOG_DB_MAPPINGS]
LOG_DB_SCOLUMNS = ",".join(LOG_DB_COLUMNS)
LOG_DB_SPLACEHOLDERS = ",".join(['%s' for x in LOG_DB_MAPPINGS])

MILE_DB_MAPPINGS = [
    [ 'v', 'v' ],
    [ 'lv', 'lv' ],
    [ 'name', 'name' ],
    [ 'uid', 'uid' ],
    [ 'race', 'race' ],
    [ 'raceabbr', 'raceabbr' ],
    [ 'cls', 'cls' ],
    [ 'char', 'charabbr' ],
    [ 'xl', 'xl' ],
    [ 'sk', 'sk' ],
    [ 'sklev', 'sklev' ],
    [ 'title', 'title' ],
    [ 'place', 'place' ],
    [ 'br', 'br' ],
    [ 'lvl', 'lvl' ],
    [ 'ltyp', 'ltyp' ],
    [ 'hp', 'hp' ],
    [ 'mhp', 'mhp' ],
    [ 'mmhp', 'mmhp' ],
    [ 'str', 'strength' ],
    [ 'int', 'intelligence' ],
    [ 'dex', 'dexterity' ],
    [ 'god', 'god' ],
    [ 'start', 'start_time' ],
    [ 'dur', 'dur' ],
    [ 'turn', 'turn' ],
    [ 'dam', 'dam' ],
    [ 'piety', 'piety' ],
    [ 'nrune', 'nrune' ],
    [ 'urune', 'urune' ],
    [ 'verb', 'verb' ],
    [ 'noun', 'noun' ],
    [ 'milestone', 'milestone' ],
    [ 'time', 'milestone_time' ],
    ]

LOGLINE_TO_DBFIELD = dict(LOG_DB_MAPPINGS)
COMBINED_LOG_TO_DB = dict(LOG_DB_MAPPINGS + MILE_DB_MAPPINGS)

R_MONTH_FIX = re.compile(r'^(\d{4})(\d{2})(.*)')
R_GHOST_NAME = re.compile(r"^(.*)'s? ghost")
R_MILESTONE_GHOST_NAME = re.compile(r"the ghost of (.*) the ")
R_KILL_UNIQUE = re.compile(r'^killed (.*)\.$')
R_MILE_UNIQ = re.compile(r'^(\w+) (.*)\.$')
R_MILE_GHOST = re.compile(r'^(\w+) the ghost of (\S+)')
R_RUNE = re.compile(r"found an? (.*) rune")
R_HYDRA = re.compile(r'^an? (?:\w+)-headed (hydra.*)')
R_ARTICLE = re.compile(r'^an? ')
R_PLACE_DEPTH = re.compile(r'^\w+:(\d+)')
R_GOD_WORSHIP = re.compile(r'^became a worshipper of (.*)\.$')
R_GOD_MOLLIFY = re.compile(r'^mollified (.*)\.$')
R_GOD_RENOUNCE = re.compile(r'^abandoned (.*)\.$')

class SqlType:
  def __init__(self, str_to_sql):
    #print str_to_sql('1')
    self.str_to_sql = str_to_sql

  def to_sql(self, string):
    return (self.str_to_sql)(string)

def fix_crawl_date(date):
  def inc_month(match):
    return "%s%02d%s" % (match.group(1), 1 + int(match.group(2)),
                         match.group(3))
  return R_MONTH_FIX.sub(inc_month, date)

class Query:
  def __init__(self, qstring, *values):
    self.query = qstring
    self.values = values

  def append(self, qseg, *values):
    self.query += qseg
    self.values += values

  def vappend(self, *values):
    self.values += values

  def execute(self, cursor):
    """Executes query on the supplied cursor."""
    self.query = self.query.strip()
    if not self.query.endswith(';'):
      self.query += ';'
    try:
      cursor.execute(self.query, self.values)
    except:
      print("Failing query: " + self.query
            + " args: " + self.values.__repr__())
      raise

  def row(self, cursor):
    """Executes query and returns the first row tuple, or None if there are no
    rows."""
    self.execute(cursor)
    return cursor.fetchone()

  def rows(self, cursor):
    self.execute(cursor)
    return cursor.fetchall()

  def count(self, cursor, msg=None, exc=Exception):
    """Executes a SELECT COUNT(foo) query and returns the count. If there is
    not at least one row, raises an exception."""
    self.execute(cursor)
    row = cursor.fetchone()
    if row is None:
      raise exc, (msg or "No rows returned for %s" % self.query)
    return row[0]

  first = count

char = SqlType(lambda x: x)
#remove the trailing 'D'/'S', fixup date
datetime = SqlType(lambda x: fix_crawl_date(x[0:-1]))
bigint = SqlType(lambda x: int(x))
sql_int = bigint
varchar = char

dbfield_to_sqltype = {
	'name':char,
	'start_time':datetime,
	'sc':bigint,
	'race':char,
        'raceabbr':char,
	'cls':char,
	'v':char,
	'lv':char,
	'uid':sql_int,
	'charabbr':char,
	'xl':sql_int,
	'sk':char,
	'sklev':sql_int,
	'title':varchar,
	'place':char,
	'branch':char,
	'lvl':sql_int,
	'ltyp':char,
	'hp':sql_int,
	'mhp':sql_int,
 	'mmhp':sql_int,
	'strength':sql_int,
	'intellegence':sql_int,
	'dexterity':sql_int,
	'god':char,
	'dur':sql_int,
	'turn':bigint,
	'urune':sql_int,
	'ktyp':char,
	'killer':char,
        'ckiller': char,
        'kaux':char,
	'dam':sql_int,
	'piety':sql_int,
        'pen':sql_int,
	'end_time':datetime,
        'milestone_time':datetime,
	'tmsg':varchar,
	'vmsg':varchar,
        'nrune':sql_int,
        'kills': sql_int,
        'gold': sql_int,
        'goldfound': sql_int,
        'goldspent': sql_int
	}

def is_selected(game):
  """Accept all games that match our version criterion."""
  return game['v'] >= OLDEST_VERSION

_active_cursor = None

def set_active_cursor(c):
  global _active_cursor
  _active_cursor = c

def active_cursor():
  global _active_cursor
  return _active_cursor

def query_do(cursor, query, *values):
  Query(query, *values).execute(cursor)

def query_first(cursor, query, *values):
  return Query(query, *values).first(cursor)

def query_first_def(cursor, default, query, *values):
  q = Query(query, *values)
  row = q.row(cursor)
  if row is None:
    return default
  return row[0]

def query_row(cursor, query, *values):
  return Query(query, *values).row(cursor)

def query_rows(cursor, query, *values):
  return Query(query, *values).rows(cursor)

def query_first_col(cursor, query, *values):
  rows = query_rows(cursor, query, *values)
  return [x[0] for x in rows]

def game_is_win(g):
  return g['ktyp'] == 'winning'

@crawl_utils.DBMemoizer
def player_exists(c, name):
  """Return true if the player exists in the player table"""
  query = Query("""SELECT name FROM players WHERE name=%s;""",
                name)
  return query.row(c) is not None

def longest_streak_count(c, player):
  return query_first_def(c, 0,
                         """SELECT streak FROM streaks
                            WHERE player = %s""",
                         player)

def update_streak_count(c, game, streak_count):
  streak_time = game['end']
  query_do(c,
           '''INSERT INTO streaks (player, streak, streak_time)
              VALUES (%s, %s, %s)
              ON DUPLICATE KEY UPDATE streak = %s, streak_time = %s''',
           game['name'], streak_count, streak_time,
           streak_count, streak_time)

def update_player_fullscore(c, player, addition, team_addition):
  query_do(c,
           '''UPDATE players
              SET score_full = score_base + %s,
                  team_score_full = team_score_base + %s
              WHERE name = %s''',
           addition, team_addition, player)

def apply_dbtypes(game):
  """Given an xlogline dictionary, replaces all values with munged values
  that can be inserted directly into a db table. Keys that are not recognized
  (i.e. not in dbfield_to_sqltype) are ignored."""
  new_hash = { }
  for key, value in game.items():
    if (COMBINED_LOG_TO_DB.has_key(key) and
        dbfield_to_sqltype.has_key(COMBINED_LOG_TO_DB[key])):
      new_hash[key] = dbfield_to_sqltype[COMBINED_LOG_TO_DB[key]].to_sql(value)
    else:
      new_hash[key] = value
  # Another pass to populate field names with SQL column names.
  augmented_hash = dict(new_hash)
  for key, value in new_hash.items():
    sqlkey = COMBINED_LOG_TO_DB.get(key)
    if sqlkey:
      augmented_hash[sqlkey] = value
  return augmented_hash

def make_xlog_db_query(db_mappings, xdict, filename, offset, table):
  fields = ['source_file']
  values = [filename]
  if offset is not None and offset != False:
    fields.append('source_file_offset')
    values.append(offset)
  for logkey, sqlkey in db_mappings:
    if xdict.has_key(logkey):
      fields.append(sqlkey)
      values.append(xdict[logkey])
  return Query('INSERT INTO %s (%s) VALUES (%s);' %
               (table, ",".join(fields), ",".join([ "%s" for v in values])),
               *values)

def insert_xlog_db(cursor, xdict, filename, offset):
  milestone = xdict.has_key('milestone')
  db_mappings = milestone and MILE_DB_MAPPINGS or LOG_DB_MAPPINGS
  thingname = milestone and 'milestone' or 'logline'
  table = milestone and 'milestones' or 'games'
  save_offset = not milestone
  query = make_xlog_db_query(db_mappings, xdict, filename,
                             save_offset and offset, table)
  try:
    query.execute(cursor)
  except Exception, e:
    error("Error inserting %s %s (query: %s [%s]): %s"
          % (thingname, milestone, query.query, query.values, e))
    raise

def update_highscore_table(c, xdict, filename, offset, table, field, value):
  existing_score = query_first_def(c, 0,
                                   "SELECT score FROM " + table +
                                   " WHERE " + field + " = %s",
                                   value)
  if xdict['sc'] > existing_score:
    if existing_score > 0:
      query_do(c, "DELETE FROM " + table + " WHERE " + field + " = %s",
               value)
    iq = make_xlog_db_query(LOG_DB_MAPPINGS, xdict, filename, offset,
                            table)
    try:
      iq.execute(c)
    except Exception, e:
      error("Error inserting %s into %s (query: %s [%s]): %s"
            % (xdict, table, iq.query, iq.values, e))
      raise

def update_highscores(c, xdict, filename, offset):
  update_highscore_table(c, xdict, filename, offset,
                         table="combo_highscores",
                         field="charabbrev",
                         value=xdict['char'])
  update_highscore_table(c, xdict, filename, offset,
                         table="class_highscores",
                         field="class",
                         value=xdict['cls'])
  update_highscore_table(c, xdict, filename, offset,
                         table="species_highscores",
                         field="raceabbr",
                         value=xdict['char'][:2])

def dbfile_offset(cursor, filename):
  """Given a db cursor and filename, returns the offset of the last
  logline from that file that was entered in the db."""
  return query_first_def(cursor, -1,
                         '''SELECT offset FROM logfile_offsets
                            WHERE filename = %s''',
                         filename)

def update_db_bookmark(cursor, table, filename, offset):
  cursor.execute('INSERT INTO ' + table + \
                   ' (source_file, source_file_offset) VALUES (%s, %s) ' + \
                   'ON DUPLICATE KEY UPDATE source_file_offset = %s',
                 [ filename, offset, offset ])

def update_milestone_bookmark(cursor, filename, offset):
  return update_db_bookmark(cursor, 'milestone_bookmark', filename, offset)

def xlog_seek(filename, filehandle, offset):
  """Given a logfile handle and the offset of the last logfile entry inserted
  in the db, seeks to the last entry and reads past it, positioning the
  read pointer at the start of the first new logfile entry."""

  info("Seeking to offset %d in logfile %s" % (offset, filename))
  if offset == -1:
    filehandle.seek(0)
  else:
    filehandle.seek(offset > 0 and offset or (offset - 1))
    # Sanity-check: the byte immediately preceding this must be "\n".
    if offset > 0:
      filehandle.seek(offset - 1)
      if filehandle.read(1) != '\n':
        raise IOError("%s: Offset %d is not preceded by newline."
                      % (filename, offset))
    else:
      filehandle.seek(offset)
    # Discard one line - the last line added to the db.
    filehandle.readline()

def extract_ghost_name(killer):
  return R_GHOST_NAME.findall(killer)[0]

def extract_milestone_ghost_name(milestone):
  return R_MILESTONE_GHOST_NAME.findall(milestone)[0]

def extract_rune(milestone):
  return R_RUNE.findall(milestone)[0]

def is_ghost_kill(game):
  killer = game.get('killer') or ''
  return R_GHOST_NAME.search(killer)

def wrap_transaction(fn):
  """Given a function, returns a function that accepts a cursor and arbitrary
  arguments, calls the function with those args, wrapped in a transaction."""
  def transact(cursor, *args):
    result = None
    cursor.execute('BEGIN;')
    try:
      result = fn(cursor, *args)
      cursor.execute('COMMIT;')
    except:
      cursor.execute('ROLLBACK;')
      raise
    return result
  return transact

def extract_unique_name(kill_message):
  return R_KILL_UNIQUE.findall(kill_message)[0]

def add_listener(listener):
  LISTENERS.append(listener)

def add_timed(interval, timed):
  TIMERS.append(CrawlTimerState(interval, timed))

def define_timer(interval, fn):
  return (interval, CrawlTimerListener(fn))

def define_cleanup(fn):
  return CrawlCleanupListener(fn)

def run_timers(c, elapsed_time):
  for timer in TIMERS:
    timer.run(c, elapsed_time)

def load_extensions():
  c = ConfigParser.ConfigParser()
  c.read(EXTENSION_FILE)

  exts = c.get('extensions', 'ext') or ''

  for ext in exts.split(','):
    key = ext.strip()
    filename = key + '.py'
    info("Loading %s as %s" % (filename, key))
    module = imp.load_source(key, filename)
    if 'LISTENER' in dir(module):
      for l in module.LISTENER:
        add_listener(l)
    if 'TIMER' in dir(module):
      for t in module.TIMER:
        add_timed(*t)

def init_listeners(db):
  for e in LISTENERS:
    e.initialize(db)

def cleanup_listeners(db):
  for e in LISTENERS:
    e.cleanup(db)

def create_master_reader():
  blacklist = Blacklist(BLACKLIST_FILE)
  processors = ([ MilestoneFile(x) for x in MILESTONES ] +
                [ Logfile(x, blacklist) for x in LOGS ])
  return MasterXlogReader(processors)

def update_xlog_offset(c, filename, offset):
  query_do(c, '''INSERT INTO logfile_offsets (filename, offset)
                      VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE offset = %s''',
           filename, offset, offset)

def process_xlog(c, filename, offset, d, flambda):
  """Processes an xlog record for scoring purposes."""
  if not is_selected(d):
    return
  # Add the player outside the transaction and suppress errors.
  def do_xlogline(cursor):
    # Tell the listeners to do their thang
    for listener in LISTENERS:
      flambda(listener)(cursor, d)
    # Update the offsets table.
    update_xlog_offset(c, filename, offset)
  wrap_transaction(do_xlogline)(c)

def process_log(c, filename, offset, d):
  """Processes a logfile record for scoring purposes."""
  return process_xlog(c, filename, offset, d,
                      lambda l: l.logfile_event)

def process_milestone(c, filename, offset, d):
  """Processes a milestone record for scoring purposes."""
  return process_xlog(c, filename, offset, d,
                      lambda l: l.milestone_event)

def scload():
  logging.basicConfig(level=logging.INFO,
                      format=crawl_utils.LOGFORMAT)

  crawl_utils.lock_or_die()
  print "Populating db (one-off) with logfiles and milestones. " + \
      "Running the scoresd.py daemon is preferred."

  load_extensions()

  db = connect_db()
  init_listeners(db)

  def proc_file(fn, filename):
    info("Updating db with %s" % filename)
    try:
      f = open(filename)
      try:
        fn(db, filename, f)
      finally:
        f.close()
    except IOError:
      warn("Error reading %s, skipping it." % log)

  cursor = db.cursor()
  set_active_cursor(cursor)
  try:
    if not OPT.no_load:
      master = create_master_reader()
      master.tail_all(cursor)
  finally:
    set_active_cursor(None)
    cursor.close()

  cleanup_listeners(db)
  db.close()

if __name__ == '__main__':
  scload()