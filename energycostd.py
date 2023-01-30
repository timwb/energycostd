#!/usr/bin/env python3
import sys, os, argparse, logging, logging.handlers, json, yaml, calendar, requests, signal, time, sched, pytz, copy
from influxdb import InfluxDBClient
from datetime import date, datetime, timedelta
import paho.mqtt.client as mqtt

configfile  = '/etc/energycost/energycostd-config.yaml'
tariffsfile = '/etc/energycost/energytariffs.yaml'

hostname    = os.uname()[1]

def reloadconfig(signum, frame):
  logger.info(f"Received signal {signum}")
  tariffs.updatetariffs()


def shutdown(signum, frame):
  logger.info(f"Received signal {signum}")


def on_connect(client, userdata, flags, rc):
  logger.info(f"MQTT client connected with result code {rc}")
  if not all([tariffs, tariffs.apx]):
    rates = tariffs.getcurrentrates()
    publish_rates(e_rate=rates[0], g_rate=rates[1])


class cl_tariffs:
  def __init__(self):
    self.updatetariffs(firstrun=True)
    self.fetchapx()
    self.fetchleba()
    self.computefinaltariffs()

  def updatetariffs(self, firstrun=False):
    """Load the right rates from tariffs yaml, compute and store result in self.tariffs
       Run every new day because new rates may apply. Also runs on SIGHUP through the 
       reloadconfig() function.
    """
    if not firstrun: oldtariffs = self.tariffs
    self.tariffs = {
      'e_annual':        0.0,
      'e_daily':         0.0,
      'e_tax_rate':      0.0,
      'g_annual':        0.0,
      'g_daily':         0.0,
      'g_tax_rate':      0.0,
      'g_corr':          1.0
    }
    self.daysinyear = 366 if calendar.isleap(datetime.now().year) else 365

    with open(tariffsfile) as tf:
      tariffs = yaml.load(tf, Loader=yaml.FullLoader)

    today = date.today()
    for entry in tariffs['vat']:
      if entry['from'] <= today and entry['to'] >= today:
        vat = entry['percentage']

    for entry in tariffs['tariffs']['electricity']['annual']:
      for entry2 in tariffs['tariffs']['electricity']['annual'][entry]:
        if entry2['from'] <= today and entry2['to'] >= today:
          logger.debug(f"Selected period from {entry2['from']:%Y-%m-%d} to {entry2['to']:%Y-%m-%d} for annual electricity cost entry '{entry}': €{entry2['amount']:.2f}")
          self.tariffs['e_annual'] += entry2['amount']
          self.tariffs['e_daily'] = self.tariffs['e_annual'] / self.daysinyear

    for entry in tariffs['tariffs']['electricity']['usage']:
      for entry2 in tariffs['tariffs']['electricity']['usage'][entry]:
        if entry2['from'] <= today and entry2['to'] >= today:
          logger.debug(f"Selected period from {entry2['from']:%Y-%m-%d} to {entry2['to']:%Y-%m-%d} for electricity usage cost entry '{entry}': {entry2['amount']*100:.1f} ct/kWh")
          self.tariffs['e_tax_rate'] += entry2['amount']

    for entry in tariffs['tariffs']['gas']['annual']:
      for entry2 in tariffs['tariffs']['gas']['annual'][entry]:
        if entry2['from'] <= today and entry2['to'] >= today:
          logger.debug(f"Selected period from {entry2['from']:%Y-%m-%d} to {entry2['to']:%Y-%m-%d} for annual gas cost entry '{entry}': €{entry2['amount']:.2f}")
          self.tariffs['g_annual'] += entry2['amount']
          self.tariffs['g_daily'] = self.tariffs['g_annual'] / self.daysinyear

    for entry in tariffs['tariffs']['gas']['usage']:
      for entry2 in tariffs['tariffs']['gas']['usage'][entry]:
        if entry2['from'] <= today and entry2['to'] >= today:
          logger.debug(f"Selected period from {entry2['from']:%Y-%m-%d} to {entry2['to']:%Y-%m-%d} for gas usage cost entry '{entry}': {entry2['amount']*100:.1f} ct/m³")
          self.tariffs['g_tax_rate'] += entry2['amount']

    for entry in self.tariffs: self.tariffs[entry] += self.tariffs[entry] * vat

    self.tariffs['g_corr'] = 1.0

    if firstrun:
      for entry in self.tariffs: logger.debug(f"Calculated tariff: '{entry}' at €{self.tariffs[entry]:.6f}")
    else:
      tariffschanged = False
      for entry in self.tariffs:
        if self.tariffs[entry] != oldtariffs[entry]:
          tariffschanged = True
          logger.info(f"Tariff changed: {entry} updated from €{oldtariffs[entry]:.6f} to €{self.tariffs[entry]:.6f}")
      if tariffschanged:
        self.computefinaltariffs()


  def fetchapx(self):
    """Call function to make get prices from apx API. Run at startup or every day at
      15:00 CET/CEST.
      WARNING Currently assumes local time zone is CET/CEST
    """
    self.baseapx =  makerequestee(config['api']['apx'])
    #self.baseapx =  makerequestez(usagetype='1')

    logger.debug("Electricity prices (inc VAT, ex. taxes):")
    for elem in self.baseapx: logger.debug(f"{elem['Timestamp'].astimezone():%Y-%m-%d %H} {elem['TariffUsage']*100:.1f} ct/kWh")


  def fetchleba(self):
    """Call function to make get prices from leba API. Run at startup or every day at
      17:00 CET/CEST.
      WARNING Currently assumes local time zone is CET/CEST
    """
    self.baseleba = makerequestee(config['api']['leba'])
    #self.baseleba = makerequestez(usagetype='3')

    logger.debug("Gas prices (inc VAT, ex taxes):")
    for elem in self.baseleba: logger.debug(f"{elem['Timestamp'].astimezone():%Y-%m-%d %H} €{elem['TariffUsage']:.2f} /m³")


  def computefinaltariffs(self):
    """Takes the tariffs and daily prices and puts them all together.
      Needs to be called whenever either of those changes.
      Also computes favorable hours, averages
    """
    today = date.today()
    now = datetime.now()
    self.apx = copy.deepcopy(self.baseapx)
    self.leba = copy.deepcopy(self.baseleba)
    for elem in self.apx:
      elem['TariffUsage'] += self.tariffs['e_tax_rate']

    for elem in self.leba:
      elem['TariffUsage'] += self.tariffs['g_tax_rate']

    if debug:
      logger.debug("Electricity prices:")
      for elem in self.apx:  logger.debug(f"{elem['Timestamp'].astimezone():%Y-%m-%d %H} {elem['TariffUsage']*100:.1f} ct/kWh")
      logger.debug("Gas prices:")
      for elem in self.leba: logger.debug(f"{elem['Timestamp'].astimezone():%Y-%m-%d %H} €{elem['TariffUsage']:.2f} /m³")

    # Compute peaks and troughs only if there is a tomorrow in the data (there might not be at startup)
    if self.apx[-1]['Timestamp'].astimezone().date() == today: return

    avg = 0.0
    n = 0

    self.apxtomorrow = []
    for elem in self.apx:
      # Do not assume days have 24 hours, due to DST
      if elem['Timestamp'].astimezone().date() == today + timedelta(days=1):
        self.apxtomorrow.append(elem)
        avg += elem['TariffUsage']
        n += 1
    self.avgtomorrow = avg / n
    logger.debug(f"Average electricity cost on {today+timedelta(days=1):%Y-%m-%d}: {self.avgtomorrow*100:.1f} ct/kWh")

    self.apxsorted = sorted(self.apxtomorrow, key = lambda i: i['TariffUsage'])

    self.peaks = []
    for i in range(1, n-1):
      if self.apxtomorrow[i-1]['TariffUsage'] <= self.apxtomorrow[i]['TariffUsage'] and self.apxtomorrow[i+1]['TariffUsage'] <= self.apxtomorrow[i]['TariffUsage']:
        self.peaks.append(self.apxtomorrow[i])

    self.troughs = []
    for i in range(1, n-1):
      if self.apxtomorrow[i-1]['TariffUsage'] >= self.apxtomorrow[i]['TariffUsage'] and self.apxtomorrow[i+1]['TariffUsage'] >= self.apxtomorrow[i]['TariffUsage']:
        self.troughs.append(self.apxtomorrow[i])

    # find cheapest 2-hour and 3-hour blocks
    self.apxdouble = []
    i = 0
    for elem in self.apx:
      if elem['Timestamp'] >= now.replace(hour=23, minute=0, second=0, microsecond=0).astimezone(pytz.utc):
        if i+1 < len(self.apx):
          self.apxdouble.append(copy.copy(elem))
          self.apxdouble[-1]['TariffUsage'] += self.apx[i+1]['TariffUsage']
      i += 1

    self.apxdoublesorted = sorted(self.apxdouble, key = lambda i: i['TariffUsage'])

    self.apxtriple = []
    i = 0
    for elem in self.apx:
      if elem['Timestamp'] >= now.replace(hour=22, minute=0, second=0, microsecond=0).astimezone(pytz.utc):
        if i+2 < len(self.apx):
          self.apxtriple.append(copy.copy(elem))
          self.apxtriple[-1]['TariffUsage'] += self.apx[i+1]['TariffUsage'] + self.apx[i+2]['TariffUsage']
      i += 1

    self.apxtriplesorted = sorted(self.apxtriple, key = lambda i: i['TariffUsage'])

    if debug:
      logger.debug("Electricity peaks:")
      for elem in self.peaks:     logger.debug(f"{elem['Timestamp'].astimezone():%Y-%m-%d %H} {elem['TariffUsage']*100:.1f} ct/kWh")
      logger.debug("Electricity troughs:")
      for elem in self.troughs:   logger.debug(f"{elem['Timestamp'].astimezone():%Y-%m-%d %H} {elem['TariffUsage']*100:.1f} ct/kWh")
      logger.debug("2-hour blocks")
      for elem in self.apxdouble: logger.debug(f"{elem['Timestamp'].astimezone():%Y-%m-%d %H} {elem['TariffUsage']*50:.1f} ct/kWh")
      logger.debug("3-hour blocks")
      for elem in self.apxtriple: logger.debug(f"{elem['Timestamp'].astimezone():%Y-%m-%d %H} {elem['TariffUsage']*33.3:.1f} ct/kWh")
      logger.debug(f"Cheapest hour: {self.apxsorted[0]['Timestamp'].astimezone():%Y-%m-%d %H} at {self.apxsorted[0]['TariffUsage']*100:.1f} ct/kWh")
      logger.debug(f"Cheapest 2 hours: {self.apxdoublesorted[0]['Timestamp'].astimezone():%Y-%m-%d %H} at {self.apxdoublesorted[0]['TariffUsage']*50:.1f} ct/kWh")
      logger.debug(f"Cheapest 3 hours: {self.apxtriplesorted[0]['Timestamp'].astimezone():%Y-%m-%d %H} at {self.apxtriplesorted[0]['TariffUsage']*33.3:.1f} ct/kWh")
      logger.debug(f"Costliest hour: {self.apxsorted[-1]['Timestamp'].astimezone():%Y-%m-%d %H} at {self.apxsorted[-1]['TariffUsage']*100:.1f} ct/kWh")
      logger.debug(f"Costliest 2 hours: {self.apxdoublesorted[-1]['Timestamp'].astimezone():%Y-%m-%d %H} at {self.apxdoublesorted[-1]['TariffUsage']*50:.1f} ct/kWh")
      logger.debug(f"Costliest 3 hours: {self.apxtriplesorted[-1]['Timestamp'].astimezone():%Y-%m-%d %H} at {self.apxtriplesorted[-1]['TariffUsage']*33.3:.1f} ct/kWh")


  def getcurrentrates(self):
    e_rate = None
    g_rate = None
    now = datetime.now()
    currenthour = now.replace(minute=0, second=0, microsecond=0).astimezone(pytz.utc)
    for elem in self.apx:
      if elem['Timestamp'] == currenthour:
        e_rate = elem['TariffUsage']
        break
    for elem in self.leba:
      if elem['Timestamp'] == currenthour:
        g_rate = elem['TariffUsage']
        break
    if e_rate == None:
      logger.warning("Could not determine current rate for electricity.")
    if g_rate == None:
      logger.warning("Could not determine current rate for gas.")
    return e_rate, g_rate

# EasyEnergy - ee
def makerequestee(baseurl):
  now = datetime.now()
  startofday = now.replace(hour=0, minute=0, second=0, microsecond=0)
  starttimestamp = startofday.astimezone(pytz.utc)
  if now.hour >= 13:
    endttimestamp = (startofday + timedelta(days=2)).astimezone(pytz.utc)
  else:
    endttimestamp = (startofday + timedelta(days=1)).astimezone(pytz.utc)

  payload = {
    'startTimestamp': starttimestamp.strftime("%Y-%m-%dT%H:00:00.000Z"),
    'endTimestamp': endttimestamp.strftime("%Y-%m-%dT%H:00:00.000Z"),
    'grouping': ''
  }

  try:
    r = requests.get(baseurl, params=payload)
  except requests.exceptions.Timeout:
    pass
    # Maybe set up for a retry, or continue in a retry loop
  except requests.exceptions.TooManyRedirects:
    pass
    # Tell the user their URL was bad and try a different one
  except requests.exceptions.RequestException as e:
    # catastrophic error. bail.
    # TODO make this more resilient.
    raise SystemExit(e)

  basetariffs = r.json()
  for elem in basetariffs:
      elem['Timestamp'] = datetime.fromisoformat(elem['Timestamp'])

  return basetariffs

# EnergyZero - ez
def makerequestez(usagetype='1'):
  baseurl = 'https://api.energyzero.nl/v1/energyprices'
  now = datetime.now()
  startofday = now.replace(hour=0, minute=0, second=0, microsecond=0)
  starttimestamp = startofday.astimezone(pytz.utc)
  if now.hour >= 13:
    endttimestamp = (startofday + timedelta(days=2)).astimezone(pytz.utc)
  else:
    endttimestamp = (startofday + timedelta(days=1)).astimezone(pytz.utc)

  payload = {
    'fromDate': starttimestamp.strftime("%Y-%m-%dT%H:00:00.000Z"),
    'tillDate': endttimestamp.strftime("%Y-%m-%dT%H:00:00.000Z"),
    'interval': '4',
    'usageType': usagetype,
    'inclBtw': 'true'
  }

  try:
    r = requests.get(baseurl, params=payload)
  except requests.exceptions.Timeout:
    pass
    # Maybe set up for a retry, or continue in a retry loop
  except requests.exceptions.TooManyRedirects:
    pass
    # Tell the user their URL was bad and try a different one
  except requests.exceptions.RequestException as e:
    # catastrophic error. bail.
    # TODO make this more resilient.
    raise SystemExit(e)

  basetariffs = []
  for elem in r.json()['Prices']:
    basetariffs.append({ 'Timestamp': elem['readingDate'], 'TariffUsage': elem['price'] })

  for elem in basetariffs:
    #2021-12-21T23:00:00Z
    elem['Timestamp'] = datetime.fromisoformat(elem['Timestamp'][:-1]+'.000+00:00')

  return basetariffs


def publish_rates(e_rate=None, g_rate=None):
  logger.debug("Publishing rates")
  if e_rate is not None:
    mqtttopic = f"{config['mqtt']['topic']}/{config['mqtt']['smartmeter_e']}/going_rate"
    if not args.dryrun:
      mqttclient.publish(mqtttopic, payload=f"{e_rate:.7f}", retain=True)
  if g_rate is not None:
    mqtttopic = f"{config['mqtt']['topic']}/{config['mqtt']['smartmeter_g']}/going_rate"
    if not args.dryrun:
      mqttclient.publish(mqtttopic, payload=f"{g_rate:.7f}", retain=True)


def publish_opportunity(opportunity):
  logger.debug("Publishing opportunity: "+opportunity)
  mqtttopic = f"{config['mqtt']['topic']}/{config['mqtt']['smartmeter_e']}/opportunity"
  if not args.dryrun:
    mqttclient.publish(mqtttopic, payload=opportunity)


def hourlytick():
  """This is the main loop"""
  logger.debug("Hourly tick")
  now = datetime.now()
  if now.hour == 0:
    tariffs.updatetariffs()
  rates = tariffs.getcurrentrates()
  publish_rates(e_rate=rates[0], g_rate=rates[1])
  nexthour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
  scheduler.enterabs(nexthour.timestamp(), 1, hourlytick)


def updateapx():
  logger.debug("Update APX")
  now = datetime.now()
  tariffs.fetchapx()
  if tariffs.baseapx[-1]['Timestamp'].astimezone().date() == now.date():
    scheduler.enter(300, 1, updateapx)
    return
  tariffs.computefinaltariffs()
  scheduler.enterabs(tariffs.apxsorted[0]['Timestamp'].timestamp(), 1, publish_opportunity, kwargs={'opportunity': 'cheapest_hour'})
  scheduler.enterabs(tariffs.apxdoublesorted[0]['Timestamp'].timestamp(), 1, publish_opportunity, kwargs={'opportunity': 'cheapest_2_hours'})
  scheduler.enterabs(tariffs.apxtriplesorted[0]['Timestamp'].timestamp(), 1, publish_opportunity, kwargs={'opportunity': 'cheapest_3_hours'})
  scheduler.enterabs(tariffs.apxsorted[-1]['Timestamp'].timestamp(), 1, publish_opportunity, kwargs={'opportunity': 'costliest_hour'})
  scheduler.enterabs(tariffs.apxdoublesorted[-1]['Timestamp'].timestamp(), 1, publish_opportunity, kwargs={'opportunity': 'costliest_2_hours'})
  scheduler.enterabs(tariffs.apxtriplesorted[-1]['Timestamp'].timestamp(), 1, publish_opportunity, kwargs={'opportunity': 'costliest_3_hours'})
  nextapx = now.replace(hour=15, minute=0, second=0, microsecond=0) + timedelta(days=1)
  scheduler.enterabs(nextapx.timestamp(), 1, updateapx)


def updateleba():
  logger.debug("Update LEBA")
  now = datetime.now()
  tariffs.fetchleba()
  if tariffs.baseleba[-1]['Timestamp'].astimezone() < now.replace(hour=23, minute=0, second=0, microsecond=0).astimezone() + timedelta(days=1):
    scheduler.enter(300, 1, updateleba)
    return
  tariffs.computefinaltariffs()
  nextleba = now.replace(hour=19, minute=0, second=0, microsecond=0) + timedelta(days=1)
  scheduler.enterabs(nextleba.timestamp(), 1, updateleba)


def main():
  """
  TODO:
  use curl 'https://mijn.easyenergy.com/nl/api/tariff/getapxtariffslasttimestamp'
  it returns: "2021-12-19T23:00:00+01:00"
  """

  now = datetime.now()
  rates = tariffs.getcurrentrates()
  publish_rates(e_rate=rates[0], g_rate=rates[1])

  if hasattr(tariffs, 'apxsorted') and tariffs.apxsorted is not None:
    scheduler.enterabs(tariffs.apxsorted[0]['Timestamp'].timestamp(), 1, publish_opportunity, kwargs={'opportunity': 'cheapest_hour'})
    scheduler.enterabs(tariffs.apxdoublesorted[0]['Timestamp'].timestamp(), 1, publish_opportunity, kwargs={'opportunity': 'cheapest_2_hours'})
    scheduler.enterabs(tariffs.apxtriplesorted[0]['Timestamp'].timestamp(), 1, publish_opportunity, kwargs={'opportunity': 'cheapest_3_hours'})
    scheduler.enterabs(tariffs.apxsorted[-1]['Timestamp'].timestamp(), 1, publish_opportunity, kwargs={'opportunity': 'costliest_hour'})
    scheduler.enterabs(tariffs.apxdoublesorted[-1]['Timestamp'].timestamp(), 1, publish_opportunity, kwargs={'opportunity': 'costliest_2_hours'})
    scheduler.enterabs(tariffs.apxtriplesorted[-1]['Timestamp'].timestamp(), 1, publish_opportunity, kwargs={'opportunity': 'costliest_3_hours'})

  nexthour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
  nextapx =  now.replace(hour=15, minute=0, second=0, microsecond=0)
  nextleba = now.replace(hour=19, minute=0, second=0, microsecond=0)
  if tariffs.baseapx[-1]['Timestamp'].astimezone().date() > now.date():
    nextapx += timedelta(days=1)
  elif now.hour >= 15:
    nextapx = now + timedelta(minutes=5)
  if tariffs.baseleba[-1]['Timestamp'].astimezone() == now.replace(hour=23, minute=0, second=0, microsecond=0).astimezone() + timedelta(days=1):
    nextleba += timedelta(days=1)
  elif now.hour >= 17:
    nextleba = now + timedelta(minutes=5)

  scheduler.enterabs(nexthour.timestamp(), 1, hourlytick)
  scheduler.enterabs(nextapx.timestamp(), 1, updateapx)
  scheduler.enterabs(nextleba.timestamp(), 1, updateleba)

  print(scheduler.queue)
  #This should never complete because there will always be jobs in the scheduler.
  scheduler.run()

  print("Jobsdone")
  time.sleep(3500)


if __name__ == '__main__':
  scriptname = os.path.splitext(os.path.basename(__file__))[0]
  parser = argparse.ArgumentParser()
  parser.add_argument('-d', '--dryrun',     help="Dry run (do not write to InfluxDB or MQTT)", action='store_true')
  parser.add_argument('-c', '--config',     help=f"Path to config file (default: {configfile})")
  parser.add_argument('-t', '--tariffs',    help=f"Path to tariffs file (default: {tariffsfile})")
  parser.add_argument('-L', '--loglevel',   help="Logging level := { CRITICAL | ERROR | WARNING | INFO | DEBUG } (default: INFO)")
  args = parser.parse_args()

  logger = logging.getLogger()

  debug = False
  if   args.loglevel == 'CRITICAL':
    own_loglevel = logging.CRITICAL
  elif args.loglevel == 'ERROR':
    own_loglevel = logging.ERROR
  elif args.loglevel == 'WARNING':
    own_loglevel = logging.WARNING
  elif (args.loglevel == 'INFO' or args.loglevel is None):
    own_loglevel = logging.INFO
  elif args.loglevel == 'DEBUG':
    own_loglevel = logging.DEBUG
    debug = True
  else:
    print(f"Unknown Log level {args.loglevel}")
    exit(1)

  logger.setLevel(own_loglevel)
  formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

  ch = logging.StreamHandler()
  ch.setLevel(own_loglevel)
  ch.setFormatter(formatter)
  logger.addHandler(ch)

  logger.info(f"*** {scriptname} starting... ***")

  if not args.config is None:
    configfile = args.config

  with open(configfile) as cfg:
    config = yaml.load(cfg, Loader=yaml.FullLoader)

  if not args.tariffs is None:
    tariffsfile = args.tariff

  scheduler = sched.scheduler(time.time, time.sleep)
  tariffs = cl_tariffs()

  #TODO put influx and mqtt in a class as well.
  influxclient = InfluxDBClient(
    host=config['influxdb']['host'],
    port=config['influxdb']['port'],
    username=config['influxdb']['username'],
    password=config['influxdb']['password'],
    database=config['influxdb']['database']
  )

  mqttclient = mqtt.Client(scriptname)
  mqttclient.on_connect = on_connect
  mqttclient.connect(config['mqtt']['host'])
  mqttclient.loop_start()

  # Set some signal handlers
  signal.signal(signal.SIGHUP, reloadconfig)
  signal.signal(signal.SIGTERM, shutdown)
  #signal.signal(signal.SIGTSTP, suspend)

  logger.info(f"*** {scriptname} running. ***")

  try:
    while True:
      main()
  except KeyboardInterrupt:
    pass

  logger.info(f"*** {scriptname} stopping. ***")
#  stats.writestate()
