import MySQLdb
import scload
import time
import crawl_utils
import sys
import query

import logging
from logging import debug, info, warn, error

import pagedefs

# Can run as a daemon and tail a number of logfiles and milestones and
# update the db.
def interval_work(cursor, interval, master):
  master.tail_all(cursor)

def tail_logfiles(logs, milestones, interval=60):
  db = scload.connect_db()
  scload.init_listeners(db)

  cursor = db.cursor()
  scload.set_active_cursor(cursor)
  elapsed_time = 0

  master = scload.create_master_reader()
  scload.bootstrap_known_raceclasses(cursor)
  try:
    while True:
      try:
        interval_work(cursor, interval, master)
        pagedefs.incremental_build(cursor)
        if not interval:
          break
      except IOError, e:
        error("IOError: %s" % e)

      time.sleep(interval)
      elapsed_time += interval

      pagedefs.tick_dirty()

      if crawl_utils.scoresd_stop_requested():
        info("Exit due to scoresd stop request.")
        break
  finally:
    scload.set_active_cursor(None)
    cursor.close()
    db.close()

if __name__ == '__main__':
  daemon = "-n" not in sys.argv

  logformat = crawl_utils.LOGFORMAT
  if daemon:
    logging.basicConfig(level=logging.DEBUG,
                        filename = crawl_utils.LOGFILE,
                        format = logformat)
  else:
    logging.basicConfig(level=logging.DEBUG, format = logformat)

  if daemon:
    crawl_utils.daemonize()
  tail_logfiles( scload.LOGS, scload.MILESTONES, 60 )
