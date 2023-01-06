This software controls the PWM pin of a raspberry pi using data from a volkszaehler server. 
It was developed in order to prioritize consumption of excess solar energy instead of feeding it back to the grid ("zero export").

Feel free to just copy random bits of code for you own project if you find something useful.

# Details

The duty cycle of the PWM pin rises if the smartmeter measures a negative power draw from the grid. The implemented controller is of type integral, so it keeps rising until the load connected to the PWM pin is able to divert the exact amount of power needed so that the smartmeter converges to a reading of zero. 

There is a web-ui while it is running, but it isn't very user friendly: settings are not persistent for example. The interface is mostly intended for monitoring the current state and trying out different controller settings (PID-coefficients, PWM frequency, load power rating) quickly without having to edit and restart the script.

It can display the power currently set by the output duty cycle and also accumulate a counter for the diverted energy so far, but the program needs to know the maximum watt rating of the connected load inorder to do this with reasonable accuracy. There's a variable for this that you can edit.

The main loop in the controller is synced to each data point send by the volkszaehler server, so the iteration frequency is determined by the data source (it's 1Hz for my smartmeter). The PID-coefficients are not independant of this rate, so don't expect the coefficients to have a formally correct scale. 

There are some nonlinear mappings before a duty cycle gets outputted: A general phase-fired-controller(PFC) compensation and a parametric curve that I tuned on my specific hardware. You probably not need these if you have a mains-power switch that is linear with respect to duty cycle (a solid state relay for example).

### volkszaehler server config

The script is expecting the volkzaehler server to send data using its InfluxDB api component, and then extracts the values in the call using some brittle parsing code. 
Here is an excerpt of the volkzaehler config, that has been tested to work with it:
```
"channels": [{
                "api": "influxdb",
                "uuid": "4a2677c0-0f1f-11ed-aa45-a39ab6e64720",
                "identifier" : "1-0:16.7.0",
                "host": "raspberry:8080",
                "database": "vzlogger",
                "measurement_name": "leistung",
                "send_uuid": false,
            }]
```

### systemd service config

If you want to run the script on boot then you can use a service unit file for systemd. I used these lines:
```
[Unit]
Description=Controlling gpio pins based on data from a volkszaehler server.

[Service]
Type=simple
ExecStart=/usr/bin/python3 /usr/local/bin/controller.py
Restart=always
RestartSec=60

[Install]
WantedBy=multi-user.target
```
# Warning

The PWM frequency is fairly high, because in my hardware setup the connected mains-power module ("Kemo ???" ) only measures the duty cycle and is not actually trying to switch the mains voltage at that frequency. It's just a PFC(phase fired controller) with a control input that understands duty cycles. 
That means if you intend to connect the PWM pin to something less fancy like a solid state relay, you may want to lower the frequency to the lowest value that your smartmeter can still compute an average over (probably something around 5Hz), inorder to minimize feeding distorted currents to the grid or in the worst case creating radio interference. 
