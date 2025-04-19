import board
import busio
#import requests
#import logging
import threading 
import firebase_admin
#import traceback

from firebase_admin import credentials, db, auth
from requests.exceptions import ConnectionError 
from time import sleep, time
from ina226 import INA226, DeviceRangeError
from gpiozero import OutputDevice
from datetime import datetime
from adafruit_bmp280 import Adafruit_BMP280_I2C
from adafruit_ahtx0 import AHTx0

# Network and Firebase credentials
WIFI_SSID = ""
WIFI_PASSWORD = ""
API_KEY = ""
USER_EMAIL = ""
USER_PASSWORD = ""
DATABASE_URL = ""  
# Firebase setup
cred_path = "/home/pi/L8/venv/battery managment system.json"
cred = credentials.Certificate(cred_path)
firebase_admin.initialize_app(cred, {'databaseURL': DATABASE_URL})
USER_UID = auth.get_user_by_email(USER_EMAIL).uid

# delay in seconds
timer_delay = 18 
# I2C instance
i2c = busio.I2C(board.SCL, board.SDA)
bmp280 = Adafruit_BMP280_I2C(i2c, address=0x77)
bmp280.sea_level_pressure = 1013.25
aht20 = AHTx0(i2c)

try:
    ina = INA226(address=0x40, shunt_ohms=0.352)
    ina.configure(avg_mode=INA226.AVG_4BIT, bus_ct=INA226.VCT_1100us_BIT, shunt_ct=INA226.VCT_1100us_BIT)
except Exception as e:
    print("INA226 init/config error:", e)
    ina = None  # Set to None so your code doesn't crash later
"""
# Take measurements
bus_voltage = ina.voltage()
shunt_voltage = ina.shunt_voltage()
current = ina.current()
power = ina.power()

# Print results
print(f"Bus Voltage: {ina.voltage():.2f} V")
print(f"Shunt Voltage: {ina.shunt_voltage():.2f} V")
print(f"Current: {ina.current():.2f} A")
print(f"Power: {ina.power():.2f} W")
"""

# GPIO se
Relay = [5,6,13]
relays = [OutputDevice(pin, active_high=True, initial_value=True) for pin in Relay]

# Initialize manual override flags for relays
manual_override = {gpio: False for gpio in Relay}  # Dictionary to track manual override status

# Sensors and actuators
def get_timestamp():
    # Ireland's timezone during winter (UTC+0) and summer (UTC+1)
    ireland_time = datetime.now()  # Uses the system's local timezone
    return int(ireland_time.timestamp())

def read_aht_sensor():
    humidity = aht20.relative_humidity
    temperature = bmp280.temperature
    if humidity is None or temperature is None:
        print("Sensor read failed")
        return None, None
    # Round the readings to 2 decimal points
    humidity = round(humidity, 2)
    temperature = round(temperature, 2)
    return humidity, temperature

def read_ina_sensor():
    if ina is None:
        return None
    try:
        shunt_voltage = round(ina.shunt_voltage(), 2)
        bus_voltage = round(ina.voltage(), 2)
        load_voltage = round((bus_voltage + shunt_voltage/1000), 2)
        return bus_voltage
    except DeviceRangeError as e:
        print(f"INA226 read error: {e}")
        return None
    except Exception as e:
        print(f"General INA226 read error: {e}")
        return None
    
def stream_callback(event):
    """
    Handles changes to the Firebase database path.
    The event contains:
    - event.event_type: The type of database event (put, patch, delete)
    - event.path: The database path where the event occurred
    - event.data: The data at the event's path
    """
    if event.data is None:
        print("No data found in the event.")
        return

    print(f"Data: {event.data}")

    # Parse the event data
    if event.path == "/":  # Root path event with full JSON payload
        try:
            gpio_states = event.data  # Data should already be a dictionary
            for gpio, state in gpio_states.items():
                gpio = int(gpio)  # Convert GPIO pin key to integer
                state = int(state)  # Convert state value to integer (0 or 1)
                if gpio in Relay:
                    relay_index = Relay.index(gpio)
                    if state == 1:
                        relays[relay_index].on()  # Turn the relay ON
                        manual_override[gpio] = True  # Enable manual override
                        print(f"GPIO {gpio} set to OFF")
                    else:
                        relays[relay_index].off()  # Turn the relay OFF
                        manual_override[gpio] = True  # Enable manual override
                        print(f"GPIO {gpio} set to ON")
                else:
                    print(f"Invalid GPIO pin: {gpio}")
        except (ValueError, TypeError) as e:
            print(f"Error parsing JSON data: {e}")

    elif event.path.startswith("/"):  # Specific GPIO pin event (e.g., /5 or /6)
        try:
            gpio = int(event.path[1:])  # Extract GPIO pin from the path
            state = int(event.data)  # Convert the state value to integer
            if gpio in Relay:
                relay_index = Relay.index(gpio)
                if state == 1:
                    relays[relay_index].on()  # Turn the relay ON
                    manual_override[gpio] = True  # Enable manual override
                    print(f"GPIO {gpio} set to OFF")
                else:
                    relays[relay_index].off()  # Turn the relay OFF
                    manual_override[gpio] = True  # Enable manual override
                    print(f"GPIO {gpio} set to ON")
            else:
                print(f"Invalid GPIO pin: {gpio}")
        except (ValueError, TypeError) as e:
            print(f"Error processing GPIO update: {e}")

def update_relay_states():
    """
    Update the state of the relays in Firebase.
    """
    gpio_states = {gpio: (1 if relay.value else 0) for gpio, relay in zip(Relay, relays)}
    db.reference("board1/outputs/digital").set(gpio_states)
    print("Relay states updated in Firebase:", gpio_states)

def init_firebase_stream():
    # Firebase Database Reference
    db_ref = db.reference("board1/outputs/digital")  # Listen to the digital output state
    while True:
        try:
            # Start listening to the database for changes
            db_ref.listen(stream_callback)
            break  # Exit loop once listener starts successfully
        except ConnectionError as e:
            print(f"Connection error: {e}")
            print("Retrying in 5 seconds...")
            time.sleep(5)  # Retry after 5 seconds
        except Exception as e:
            print(f"Unexpected error: {e}")
            time.sleep(5)  # Retry after 5 seconds    
        
def main_loop():
    last_send_time = time()
    # Define threshold values
    LOW_THRESHOLD = 13.2   # Voltage at which the charger turns ON
    HIGH_THRESHOLD = 14.3  # Voltage at which the charger turns OFF

    # Persistent state to track whether the charger should be on or off
    charger_on = False

    while True:
        humidity, temperature = read_aht_sensor()
        bus_voltage = read_ina_sensor()
        
        # Check if relay 6 is active (indicating manual override)
        if relays[Relay.index(6)].value:
            manual_override[5] = False
            charger_on = False
        else:
            manual_override[5] = True
            charger_on = True
        
        # If not manually overridden, apply ATS logic
        if not manual_override[5]:  
            if bus_voltage is not None:
                if bus_voltage < LOW_THRESHOLD and not charger_on:
                    relays[Relay.index(5)].off()  # Turn ON the charger (relay 5 is active-low)
                    charger_on = True
                    # print("Battery voltage low. Charger ON.")
                
                elif bus_voltage > HIGH_THRESHOLD and not charger_on:
                    relays[Relay.index(5)].on()  # Turn OFF the charger
                    charger_on = False
                    # print("Battery voltage high. Charger OFF.")
        
                
                
        if time() - last_send_time > timer_delay:
            last_send_time = time()
            timestamp = get_timestamp()
            if humidity and temperature and bus_voltage:
                data = {
                    "temperature": temperature,
                    "humidity": humidity,
                    "voltage": bus_voltage,
                    "timestamp": timestamp,
                }
                ref_path = f"/UsersData/{USER_UID}/readings/{timestamp}"
                db.reference(ref_path).set(data)
                print("Data uploaded:", data)

                # Update relay states in Firebase0
                update_relay_states()
                # Print results
                print(f"Bus Voltage: {ina.voltage():.2f} V")
                print(f"Shunt Voltage: {ina.shunt_voltage():.2f} V")
                print(f"Current: {ina.current():.2f} A")
                print(f"Power: {ina.power():.2f} W")
            else:
                print("Sensor read failed")
        sleep(1)


# Main execution
if __name__ == "__main__":
    # Start Firebase stream listener in a separate thread
    stream_thread = threading.Thread(target=init_firebase_stream)
    stream_thread.daemon = True  # Allow the thread to exit when the main program ends
    stream_thread.start()

    print("Listening for Firebase changes...")

    while True:
        try:
            main_loop()  # This should be a long-running function
        except KeyboardInterrupt:
            print("Exiting by user...")
            break
        except Exception as e:
            print(f"Error in main loop: {e}")
            print("Restarting main_loop in 5 seconds...")
            time.sleep(5)  # Wait before retrying



