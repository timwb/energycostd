# energycostd

## What it does
Energycostd fetches daily electricity and gas prices from Dutch dynamic rate energy providers (such as Easy Energy, Tibber, Energy Zero, Frank energie, Nieuwe Stroom, Opensource Energie, NextEnergy, NewEnergy, Elix) and then:

- Adds taxes and publishes the current price on a MQTT topic of your choice
- Computes some stats for electricity and logs these
- Publishes "opportunities" for electricity - the cheapest and most expensive 1, 2 and 3 hour blocks

This is useful if:

- You automatically want to control devices based on prices with your home automation software of choice.
- You want to have dynamic prices in Home Assistant. In this case you need to add a sensor of class 'monetary':

```yaml
mqtt:
  sensor:
    - unique_id: "E1234567890123456_going_rate"  
      name: "EPEX NL Current rate + tax"
      device_class: "monetary"
      state_topic: "energycost/E1234567890123456/going_rate"
      unit_of_measurement: "EUR/kWh"
    - unique_id: "G0078565595750920_going_rate"  
      name: "LEBA Current rate + tax"
      device_class: "monetary"
      state_topic: "energycost/G1234567890123456/going_rate"
      unit_of_measurement: "EUR/m³"
```

## How to use

- It expects the yaml files to be in /etc/energycost by default
- The current values in energytariffs.yaml are based on Stedin and some Easy Energy peculiarities, adjust to your situation.
- Install Python dependencies as needed
- Use the included systemd unit file for inspiration :)


## Known issues

- Unpredictable behavior when the API is unreachable
- Crashes when it runs out of prices to publish. This happens frequently with gas prices on long weekends.
- Unpredictable behavior when switching from/to DST

## TODO

- Start adding prices to an Influxdb
- Not crash for at least 1 year straight
- Fetch historical prices and add those to Influxdb as well

## Warnings

I am not a software developer, i smash the keyboard for long enough until it works for me. If this code hurts your eyes / costs you money / eats your homework / becomes sentient and destroys civilization, I am in no way responsible.

## Contributing

Yes please!