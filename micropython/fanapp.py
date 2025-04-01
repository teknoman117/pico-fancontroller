# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from machine import Pin, PWM, Timer
from neopixel import NeoPixel

from rgb_hsv_conversion import hsv_to_rgb

import math
import micropython
import os
import rp2
import time

# Main Application
class FanApp:
    STATE_Idle = const(0)
    STATE_Low = const(1)
    STATE_LowPlus = const(2)
    STATE_Medium = const(3)
    STATE_MediumPlus = const(4)
    STATE_High = const(5)
    
    # LED in "Battery" state turns off after 5 seconds
    # LED in "Charging" state "breathes"
    # LED in "PluggedIn" state just stays on
    POWER_STATE_Battery = const(0)
    POWER_STATE_Charging = const(1)
    POWER_STATE_PluggedIn = const(2)
    
    COLORS = [(0, 0, 1), (240, 1, 1), (180, 1, 1), (120, 1, 1), (60, 1, 1), (0, 1, 1)]
    SPEEDS = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
    MAX_BRIGHTNESS = 0.125
    
    def __init__(self):
        # setup hardware
        self.neopixel = NeoPixel(Pin(23), 1)
        self.standby = Pin(0, Pin.IN, pull=Pin.PULL_UP)
        self.standby.irq(handler=lambda p: micropython.schedule(self.power_state_cb, p))
        self.charging = Pin(1, Pin.IN, pull=Pin.PULL_UP)
        self.charging.irq(handler=lambda p: micropython.schedule(self.power_state_cb, p))
        self.speed_button = Pin(24, Pin.IN, pull=Pin.PULL_UP)
        self.speed_button.irq(trigger=Pin.IRQ_FALLING, handler=lambda p: micropython.schedule(self.speed_button_cb, p))
        
        self.fan_tachometer = PulseCounter(0, Pin(14, Pin.IN, pull=Pin.PULL_UP))
        self.fan = PWM(Pin(15), freq=25_000)
        self.fan.duty_u16(0)
        
        self.neopixel_timer = Timer()
        self.neopixel_animating = False
        self.neopixel_animating_start = 0
    
        # attempt to restore previous state
        try:
            with open("state.txt", "r") as f:
                self.state = int(f.read().strip())
        except (OSError, ValueError):
            self.state = self.STATE_Idle

    # Decode the battery charger status inputs (active-Low signals)
    def get_power_state(self):
        if not self.charging.value():
            return self.POWER_STATE_Charging
        elif not self.standby.value():
            return self.POWER_STATE_PluggedIn
        else:
            return self.POWER_STATE_Battery

    # Called whenever the speed select button is pressed
    def speed_button_cb(self, _):
        # if we're on battery and the light has gone off, illuminate it the first time the button is pressed
        if self.get_power_state() == self.POWER_STATE_Battery and not self.neopixel_animating:
            self.apply_state()
            return

        # otherwise, advance the speed state
        self.state = self.state + 1
        if self.state > self.STATE_High:
            self.state = self.STATE_Idle
        self.apply_state()
    
    # Called whenever the battery charger status inputs change
    def power_state_cb(self, _):
        self.apply_state()
    
    def set_fan(self, speed):
        self.fan.duty_u16(int(speed * 65535))
        
    # set the LED's color based on the current state with the supplied brightness
    def set_led(self, brightness):
        h, s, v = self.COLORS[self.state]
        r, g, b = hsv_to_rgb(h, s, v)
        r = int(r * brightness)
        g = int(g * brightness)
        b = int(b * brightness)
        self.neopixel[0] = (r, g, b)
        self.neopixel.write()
    
    # timer callback for the "on battery" LED animation
    def led_animation_battery_cb(self, _):
        self.set_led(0.0)
        self.neopixel_animating = False
    
    # start the "on battery" LED animation - turn on full brightness for 5 seconds then turn off
    def set_led_animation_battery(self):
        self.set_led(self.MAX_BRIGHTNESS)
        self.neopixel_animating = True
        self.neopixel_timer.init(mode=Timer.ONE_SHOT, period=5000, callback=lambda t: micropython.schedule(self.led_animation_battery_cb, t))
    
    # timer callback for the "charging" LED animation
    def led_animation_charging_cb(self, _):
        diff = float(time.ticks_diff(time.ticks_ms(), self.neopixel_animating_start)) / 1000.0
        self.set_led(self.MAX_BRIGHTNESS * abs(math.cos(diff)))
    
    # start the "charging" LED animation - pulse the LED on a ~3 second period (Pi seconds)
    def set_led_animation_charging(self):
        self.neopixel_animating_start = time.ticks_ms()
        self.neopixel_animating = True
        self.neopixel_timer.init(mode=Timer.PERIODIC, period=10, callback=lambda t: micropython.schedule(self.led_animation_charging_cb, t))
        
    # start the "plugged in" LED animation - just turn the LED on
    def set_led_animation_pluggedin(self):
        self.set_led(self.MAX_BRIGHTNESS)
        if self.neopixel_animating:
            self.neopixel_timer.deinit()
        self.neopixel_animating = False
        
    # set fan speed and led animation based on the current speed state and power state
    def apply_state(self):
        # set fan speed based on state
        self.set_fan(self.SPEEDS[self.state])
        
        # set LED animation based on power state
        power_state = self.get_power_state()
        if power_state == self.POWER_STATE_Battery:
            self.set_led_animation_battery()
        elif power_state == self.POWER_STATE_Charging:
            self.set_led_animation_charging()
        elif power_state == self.POWER_STATE_PluggedIn:
            self.set_led_animation_pluggedin()
        
        # persist fan speed
        with open("state.txt", "w") as f:
            f.write(str(self.state))
    
    # start the "app" by kicking off state application
    def start(self):
        self.apply_state()

# PIO state machine for counting pulses
@rp2.asm_pio()
def pulse_counter():
    label("loop")
    # We wait for a rising edge
    wait(0, pin, 0)
    wait(1, pin, 0)
    jmp(x_dec, "loop")  # If x is zero, then we'll wrap back to beginning


class PulseCounter:
    # pin should be a machine.Pin instance
    def __init__(self, sm_id, pin):
        self.sm = rp2.StateMachine(sm_id, pulse_counter, in_base=pin)
        # Initialize x to zero
        self.sm.put(0)
        self.sm.exec("pull()")
        self.sm.exec("mov(x, osr)")
        # Start the StateMachine's running.
        self.sm.active(1)

    def get_pulse_count(self):
        self.sm.exec("mov(isr, x)")
        self.sm.exec("push()")
        # Since the PIO can only decrement, convert it back into +ve
        return -self.sm.get() & 0x7fffffff
