#!/usr/bin/env python
# -*- coding: utf-8 -*-
import time
import sys
import os
import re
import rrdtool
from optparse import OptionParser
from mailng.lib import getoption
from mailng.admin.models import Domain
import grapher

"""
Postfix log parser.


"""

rrdstep = 60
xpoints = 540
points_per_sample = 3

class LogParser(object):
    def __init__(self, logfile, workdir,
                 year=None, debug=False, verbose=False):
        self.logfile = logfile
        try:
            self.f = open(logfile)
        except IOError:
            sys.exit(1)
#         except (IOError, errno, strerror):
#             print "[rrd] I/O error({0}): {1} ".format(errno, strerror)+logfile
#             return None
        self.workdir = workdir
        self.year = year
        self.debug = debug
        self.verbose = verbose
        self.cfs = ['AVERAGE', 'MAX']
        
        self.last_month = None
        if not self.year:
            self.year = time.localtime().tm_year
        self.data = {}
        domains = Domain.objects.all()
        self.domains = []
        for dom in domains:
            self.domains += [str(dom.name)]
            self.data[str(dom.name)] = {}
        self.data["global"] = {}

        self.workdict = {}
        self.lupdates = {}
        self.line_expr = re.compile("(\w+)\s+(\d+)\s+(\d+):(\d+):(\d+)\s+(\w+)\s+(\w+)/?\w*[[](\d+)[]]:\s+(.*)")

    def str2Time(self, y, M, d, h, m, s):
        """str2Time

        return epoch time from Year Month Day Hour:Minute:Second time format
        """
        try:
            local = time.strptime("%s %s %s %s:%s:%s" %(y, M, d, h, m, s), \
                                      "%Y %b %d %H:%M:%S")
        except:
            print "[rrd] ERROR unrecognized %s time format" %(y, M, d, h, m, s)
            return 0
        return int(time.mktime(local))

    def init_rrd(self, fname, m):
        """init_rrd

        Set-up Data Sources (DS)
        Set-up Round Robin Archives (RRA):
        - day,week,month and year archives
        - 2 types : AVERAGE and MAX

        parameter : start time
        return    : last epoch recorded
        """
        ds_type = 'ABSOLUTE'
        rows = xpoints / points_per_sample
        realrows = int(rows * 1.1)    # ensure that the full range is covered
        day_steps = int(3600 * 24 / (rrdstep * rows))
        week_steps = day_steps * 7
        month_steps = week_steps * 5
        year_steps = month_steps * 12

        # Set up data sources for our RRD
        params = []
        for v in ["sent", "recv", "bounced", "reject"]:
            params += ['DS:%s:%s:%s:0:U' % (v, ds_type, rrdstep * 2)]

        # Set up RRD to archive data
        rras = []
        for cf in ['AVERAGE', 'MAX']:
            for step in [day_steps, month_steps, month_steps, year_steps]:
                params += ['RRA:%s:0.5:%s:%s' % (cf, step, realrows)]

        # With those setup, we can now created the RRD
        rrdtool.create(fname,
                       '--start', str(m),
                       '--step', str(rrdstep),
                       *params)
        return m

    def update_rrd(self, dom, t):
        """update_rrd

        Update RRD with records at t time.

        True  : if data are up-to-date for current minute
        False : syslog may have probably been already recorded
        or something wrong
        """
        fname = "%s/%s.rrd" % (self.workdir, dom)
        m = t - (t % rrdstep)
        if not os.path.exists(fname):
            self.lupdates[fname] = self.init_rrd(fname, m)
            print "[rrd] create new RRD file"
        else:
            if not self.lupdates.has_key(fname):
                self.lupdates[fname] = rrdtool.last(fname)

        if m <= self.lupdates[fname]:
            if self.verbose:
                print "[rrd] VERBOSE events at %s already recorded in RRD" %m
            return False

        # Missing some RRD steps
        # Est ce vraiment nécessaire... ?
        if m > self.lupdates[fname] + rrdstep:
            for p in range(self.lupdates[fname] + rrdstep, m, rrdstep):
                if self.verbose:
                    print "[rrd] VERBOSE update %s:%s:%s:%s:%s (SKIP)" \
                          %(p,'0','0','0','0')
                rrdtool.update(fname, "%s:%s:%s:%s:%s" \
                                   % (p, '0', '0', '0', '0'))

        if self.verbose:
            print "[rrd] VERBOSE update %s:%s:%s:%s:%s" \
                  %(m, self.data[dom][m]['sent'], self.data[dom][m]['recv'],\
                    self.data[dom][m]['bounced'], self.data[dom][m]['reject'])

        rrdtool.update(fname, "%s:%s:%s:%s:%s" \
                           % (m, self.data[dom][m]['sent'],  
                              self.data[dom][m]['recv'],
                              self.data[dom][m]['bounced'], 
                              self.data[dom][m]['reject']))
        self.lupdates[fname] = m
        return True

    def inc_counter(self, dom, cur_t, counter):
        if not self.data[dom].has_key(cur_t):
            self.data[dom][cur_t] = \
                {'sent' : 0, 'recv' : 0, 'bounced' : 0, 'reject' : 0}
        self.data[dom][cur_t][counter] += 1

        if not self.data["global"].has_key(cur_t):
            self.data["global"][cur_t] = \
                {'sent' : 0, 'recv' : 0, 'bounced' : 0, 'reject' : 0}
        self.data["global"][cur_t][counter] += 1

    def process(self):
        for line in self.f.readlines():
            m = self.line_expr.match(line)
            if not m:
                continue
            (mo, da, ho, mi, se, host, prog, pid, log) = m.groups()

            se = int(int(se) / rrdstep)            # rrd step is one-minute => se = 0
            cur_t = self.str2Time(self.year, mo, da, ho, mi, se)
            cur_t = cur_t - cur_t % rrdstep

            m = re.search("(\w{10}): from=<(.*)>", log)
            if m:
                self.workdict[m.group(1)] = {'from' : m.group(2)}
                continue

            m = re.search("(\w{10}): to=<(.*)>.*status=(\S+)", log)
            if m:
                if not self.workdict.has_key(m.group(1)):
                    print "Inconsistent mail, skipping"
                    continue
                addrfrom = re.match("([^@]+)@(.+)", self.workdict[m.group(1)]['from'])
                if addrfrom and addrfrom.group(2) in self.domains:
                    self.inc_counter(addrfrom.group(2), cur_t, 'sent')
                addrto = re.match("([^@]+)@(.+)", m.group(2))
                if addrto.group(2) in self.domains:
                    if m.group(3) == "sent":
                        self.inc_counter(addrto.group(2), cur_t, 'recv')
                    else:
                        self.inc_counter(addrto.group(2), cur_t, m.group(3))
                continue
            
            m = re.search("NOQUEUE: reject: .*from=<(.*)> to=<([^>]*)>", log)
            if m:
                addrto = re.match("([^@]+)@(.+)", m.group(2))
                if addrto and addrto.group(2) in self.domains:
                    self.inc_counter(addrto.group(2), cur_t, 'reject')
                continue
        
        # Sort everything by time
        G = grapher.Grapher()
        for dom, data in self.data.iteritems():
            sortedData = {}
            sortedData = [ (i, data[i]) for i in sorted(data.keys()) ]
            for t, dict in sortedData:
                self.update_rrd(dom, t)
            G.make_defaults(dom)

if __name__ == "__main__":
    log_file = getoption("LOGFILE", "/var/log/maillog")
    rrd_rootdir = getoption("RRD_ROOTDIR", "/tmp")

    parser = OptionParser()
    parser.add_option("-t", "--target", default="all",
                      help="Specify which target handled while parsing log file (default to all)")
    parser.add_option("-l","--logFile", default=log_file,
                      help="postfix log in syslog format", metavar="FILE")
    parser.add_option("-v","--verbose", default=False, action="store_true", 
                      dest="verbose", help="set verbose mode")
    parser.add_option("-d","--debug", default=False, action="store_true", 
                      dest="debug", help="set debug mode")
    (options, args) = parser.parse_args()
  
    P = LogParser(options.logFile, rrd_rootdir,
                  debug=options.debug, verbose=options.verbose)
    P.process()