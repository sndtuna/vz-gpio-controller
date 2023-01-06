#!/usr/bin/env python3

import sys
import signal
import time
from time import sleep
import math
import requests
from requests.exceptions import ConnectTimeout
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
import wiringpi

settings = {
    "Kp_per_timestep": 0.14, # proportional controller: control error is feed straight to the output.
    "Ki_per_timestep": 0.4, # integral controller: control error accumulates for each loop iteration.
    "Kd_per_timestep": 0.0, # derivative of control error added to the output.
    "Ki_upwards_gain": 1.0,
    "Ki_downwards_gain": 1.0,
    "load_rated_watts": 1650, # default rated power of the load in watts.
    "meter_target_watts": -15.0, # controller aims to regulate the power meter reading to this value.
    "estimated_watt_hours_so_far": 0.0,
    "pin_out": 17,
    "pin_pwm": 12,
    "pwm_range": 4096, # maximum is 4096.
    "pwm_clk_div": 8, # maximum is 4095.
}

live_state = { #readonly variables to be displayed on the web interface.
    "estimated_output_power_watts": 0.0,
    "elapsed_time_history": [],
    "elapsed_time_history_max": None,
    "elapsed_time_history_min": None,
    "elapsed_time_history_median": None,
}
was_settings_change_request = False # global variable to signal that last http request was a settings change.

html_root = """
<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <title>Load Controller</title>
  </head>
  <body>
{html_controller_state}
{html_settings_form} 
  </body>
</html>
"""
html_controller_state = """
    <h2>Load Controller</h2>
    Current power: {estimated_output_power_watts}W <br><br>
    <h4>Input data time period statistics</h4>
    max:    {elapsed_time_history_max} <br>
    min:    {elapsed_time_history_min} <br>
    median: {elapsed_time_history_median} <br><br>
    rawdata: {elapsed_time_history} <br><br><br>
"""
html_settings_form = """
    <h4>Settings:</h4>
      <form method="get">
        <label for="load_rated_watts">Load rated power: ({load_rated_watts}) </label>
        <input type="number" id="load_rated_watts" name="load_rated_watts" min="1"><br><br>
        <label for="Kp_per_timestep">P-control (Kp): ({Kp_per_timestep}) </label>
        <input type="number" id="Kp_per_timestep" name="Kp_per_timestep" step="any"><br><br>
        <label for="Ki_per_timestep">I-control (Ki per timestep): ({Ki_per_timestep}) </label>
        <input type="number" id="Ki_per_timestep" name="Ki_per_timestep" step="any"><br><br>
        <label for="Kp_per_timestep">D-component (Kd): ({Kd_per_timestep}) </label>
        <input type="number" id="Kd_per_timestep" name="Kd_per_timestep" step="any"><br><br>
        <label for="estimated_watt_hours_so_far">Energy counter(Wh): ({estimated_watt_hours_so_far}) </label>
        <input type="number" id="estimated_watt_hours_so_far" name="estimated_watt_hours_so_far"><br><br>
        <label for="pwm_clk_div">PWM clock divider: ({pwm_clk_div}) </label>
        <input type="number" id="pwm_clk_div" name="pwm_clk_div" min="0" max="4095"><br><br>
        <label for="meter_target_watts">Grid power draw target: ({meter_target_watts}) </label>
        <input type="number" id="meter_target_watts" name="meter_target_watts"><br><br>
        <input type="submit" value="Submit">
      </form>
"""

class MyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        print("request for settings page received.")
        query = urlparse(self.path).query
        parameters = parse_qs(query)
        print("submitted parameters: "+str(parameters))
        error_message = None
        try:
            parsed = dict((k, float(v[0])) for (k, v) in parameters.items())
            settings.update(parsed)
            if len(parsed) > 0: # is the browser just visiting the page(0) or submitting settings(>0) ?
                self.send_response(303) # prevent the browser from displaying the GET parameter list.
                self.send_header("Location", "/")
            else:
                self.send_response(200)
        except Exception as e:
            self.send_response(200)
            error_message = "Error in parameters. The html form input was not able to prevent this type of error: " + str(e)
            print(e)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        if error_message:
            self.wfile.write(error_message.encode())
        global estimated_output_power_watts
        #html_state = html_controller_state.format(estimated_output_power_watts=int(estimated_output_power_watts), elapsed_time_history=str(elapsed_time_history))
        html_state = html_controller_state.format_map(live_state)
        html_settings = html_settings_form.format_map(settings)
        html_assembled = html_root.format(html_controller_state=html_state, html_settings_form=html_settings)
        self.wfile.write(html_assembled.encode())
        global was_settings_change_request
        was_settings_change_request = True

    def do_POST(self):
        self.send_response(204)
        self.end_headers()
        data_string = self.rfile.read().decode()
        lines = data_string.split('\n')
        last_line = lines[-2]
        key_value = last_line.split(' ')[1]
        value = key_value.split('=')[1]
        global meter_watts
        global was_settings_change_request
        meter_watts = float(value)
        was_settings_change_request = False

    def log_request(code, size):
        pass


class MyHTTPServer(HTTPServer):
    timeout = 10.0 # should be shorter than 60sec in order to catch SIGTERM from "systemd stop service" fast enough.
    def handle_timeout(self):
        print("no reception of volkszaehler data.")
        global meter_watts
        meter_watts = None


def update_elapsed_time_history_stats(elapsed_time_seconds):
    global live_state
    history_buffer_size = 11
    live_state["elapsed_time_history"].append(elapsed_time_seconds)
    while len(live_state["elapsed_time_history"]) > history_buffer_size:
        live_state["elapsed_time_history"].pop(0)
        live_state["elapsed_time_history_max"] = max(live_state["elapsed_time_history"])
        live_state["elapsed_time_history_min"] = min(live_state["elapsed_time_history"])
        live_state["elapsed_time_history_median"] = sorted(live_state["elapsed_time_history"])[history_buffer_size//2]


sigterm_received = False
def handler(signum, frame):
    global sigterm_received
    sigterm_received = True
    print('SIGTERM received.')


signal.signal(signal.SIGTERM, handler)
wiringpi.wiringPiSetupGpio()
wiringpi.pinMode(settings["pin_out"], 1)       # 1 means OUTPUT
wiringpi.pinMode(settings["pin_pwm"], 2)       # 2 means PWM
wiringpi.pwmSetMode(wiringpi.PWM_MODE_MS) # Use fixed frequency PWM.
wiringpi.pwmWrite(settings["pin_pwm"], 0)
wiringpi.pwmSetClock(int(settings["pwm_clk_div"]))
wiringpi.pwmSetRange(int(settings["pwm_range"]))
for i in range(3):
    wiringpi.digitalWrite(settings["pin_out"], 1)
    sleep(0.5)
    wiringpi.digitalWrite(settings["pin_out"], 0)
    sleep(0.5)

try:
    server = MyHTTPServer(('0.0.0.0', 8080), MyHandler)
    controller_integral_state = 0.0
    meter_watts = 0.0
    delta_normalized = 0.0
    elapsed_time_prev = None
    energy_accumulation_per_iter_start_time = time.monotonic()
    while not sigterm_received:
        # wait for a client (volkszaehler) to send the current power meter readings.
        server.handle_request() # reading is stored in the global meter_watts variable.
        # TODO: is there some other way to return information instead of global variables?
        if was_settings_change_request:
            wiringpi.pwmSetClock(int(settings["pwm_clk_div"]))
            wiringpi.pwmSetRange(int(settings["pwm_range"]))
            continue # changes to settings wont cause an additional tick for the controller.

        # measure loop iteration time for energy accumulation estimate
        elapsed_time_seconds = time.monotonic() - energy_accumulation_per_iter_start_time
        settings["estimated_watt_hours_so_far"] += elapsed_time_seconds * live_state["estimated_output_power_watts"] / 3600
        update_elapsed_time_history_stats(elapsed_time_seconds)
        energy_accumulation_per_iter_start_time = time.monotonic()

        # report excessive jitter in data input frequency, because the controller was designed for a fixed period.
        if elapsed_time_prev is not None and not elapsed_time_prev == 0.0: 
            if abs(elapsed_time_seconds-elapsed_time_prev)/elapsed_time_prev >= 0.20:
                print("controller update period deviated by more than 20%: "+str(elapsed_time_seconds), flush=True)
        elapsed_time_prev = elapsed_time_seconds

        # turn power load off in case of a dropped data stream connection
        if meter_watts is None:
            print("switching power output off.")
            wiringpi.pwmWrite(settings["pin_pwm"], 0)
            estimated_output_power_watts = 0
            continue
        
        # update state of the integral controller
        delta_watts = settings["meter_target_watts"] - meter_watts
        delta_normalized_prev = delta_normalized
        delta_normalized = delta_watts / settings["load_rated_watts"]
        if delta_normalized >= 0.0:
            adjustment = delta_normalized * settings["Ki_upwards_gain"] * settings["Ki_per_timestep"]
        else: 
            adjustment = delta_normalized * settings["Ki_downwards_gain"] * settings["Ki_per_timestep"]
        controller_integral_state = controller_integral_state + adjustment
        controller_integral_state = max(0.0, min(1.0, controller_integral_state))
        controller_p_state = delta_normalized * settings["Kp_per_timestep"]
        controller_d_state = (delta_normalized - delta_normalized_prev) * settings["Kd_per_timestep"]
        controller_output = controller_integral_state + controller_p_state + controller_d_state
        controller_output = max(0.0, min(1.0, controller_output))
        
        # apply inverse phase fired controller nonlinearity before writing output.
        # you can skip these steps if you use a solid state relay.
        power_ratio = controller_output
        live_state["estimated_output_power_watts"] = power_ratio * settings["load_rated_watts"]
        pfc_comp = math.acos(1-power_ratio*2.0)/math.pi
        # TODO: not actually correct. This compensation was derived from the average voltage
        # over the pfc'ed sinewave, but it needs to be about the average power, which has a 
        # additional squareing operation inside the integral. The correct formula would be 
        # the inverse of x-0.5*sin(2pi*x)/pi, which has no closed solution. It would require 
        # a numerical solver, which is a longer task I'll defer for later.

        # load specific nonlinearities compensation. 
        # set both of these values to 1.0 to have a linear curve for an ideal load (eg. temp independant resistor).
        upper = 0.67
        lower = 0.88
        upper_end_comp = 1-pow(1-pfc_comp, upper)
        lower_end_comp = pow(upper_end_comp, lower)

        # convert to integer range for wiringpi library / hardware interface
        pwm_ratio = lower_end_comp
        pwm_int_range = int(pwm_ratio * settings["pwm_range"])
        wiringpi.pwmWrite(settings["pin_pwm"], pwm_int_range)
        if delta_watts > 0.0:
            wiringpi.digitalWrite(settings["pin_out"], 1)
        else:
            wiringpi.digitalWrite(settings["pin_out"], 0)
            
        # console debug output 
        if "--debug" in sys.argv:
            print("nominal power(estimated): "+str(settings["load_rated_watts"]))
            print("power meter target: "+str(int(settings["meter_target_watts"])))
            print("meter_watts: "+str(int(meter_watts)))
            print("delta_watts: "+str(int(delta_watts)))
            print("delta_normalized: "+str(delta_normalized))
            print("adjustment: "+str(adjustment))
            print("controller_p_state: "+str(controller_p_state))
            print("controller_integral_state: "+str(controller_integral_state))
            print("controller_d_state: "+str(controller_d_state))
            print("normalized power: "+str(power_ratio))
            print("current power(estimated): "+str(int(live_state["estimated_output_power_watts"])))
            print("pwm_ratio: "+str(pwm_ratio))
            print("controller time step period: "+str(elapsed_time_seconds))
            print("accumulated energy(estimated): "+str(int(settings["estimated_watt_hours_so_far"])))
            print()

except (KeyboardInterrupt) as e:
    print(e)
server.socket.close()
print('server has shutdown.')
wiringpi.digitalWrite(settings["pin_out"], 0)
wiringpi.pwmWrite(settings["pin_pwm"], 0)
print('gpio pins have been zeroed.')
