# Import required libraries for hardware interfacing and Firebase
import board  # For board pin definitions (SCL, SDA)
import busio  # For I2C communication
#import requests  # Not used, kept for future HTTP requests
import logging  # For logging system status and events
import threading  # For running Firebase listener in a separate thread
import firebase_admin  # Firebase Admin SDK to access database and authentication

# from firebase_admin we import specific modules
from firebase_admin import credentials, db, auth  # To authenticate and interact with Firebase DB
from requests.exceptions import ConnectionError  # To catch network-related exceptions
from time import sleep, time  # sleep() for delays, time() for timestamps
from ina226 import INA226, DeviceRangeError  # Library to interact with INA226 current sensor
from gpiozero import OutputDevice  # To control relays via GPIO pins
from datetime import datetime  # To get timestamps in human-readable form
from adafruit_bmp280 import Adafruit_BMP280_I2C  # For BMP280 temperature sensor over I2C
from adafruit_ahtx0 import AHTx0  # For AHT20 temperature and humidity sensor

#%%%%%%%%%%%%%%%%%%%% Wi-Fi, Firebase Setup, and I2C Sensor Initialization   %%%%%%%%%%%%%%%%%%%%%%

# Wi-Fi and Firebase credentials (used during authentication)
WIFI_SSID = ""
WIFI_PASSWORD = ""
API_KEY = ""
USER_EMAIL = ""
USER_PASSWORD = ""

# Firebase Realtime Database URL
DATABASE_URL = "https://battery-management-syste-5a0ab-default-rtdb.europe-west1.firebasedatabase.app"

# Path to Firebase Admin SDK JSON credential file
cred_path = "/home/pi/L8/venv/battery managment system.json"

# Authenticate and initialize Firebase Admin SDK
cred = credentials.Certificate(cred_path)  # Load the service account credentials
firebase_admin.initialize_app(cred, {'databaseURL': DATABASE_URL})  # Initialize app with DB

# Get the UID of the user (used to identify whose data we are uploading to Firebase)
USER_UID = auth.get_user_by_email(USER_EMAIL).uid

# Define delay for sending data to Firebase
timer_delay = 18  # seconds

# Initialize I2C bus on Raspberry Pi
i2c = busio.I2C(board.SCL, board.SDA)  # Create I2C bus using SCL and SDA pins

# Initialize BMP280 temperature sensor
bmp280 = Adafruit_BMP280_I2C(i2c, address=0x77)
bmp280.sea_level_pressure = 1013.25  # Set sea-level pressure for altitude compensation

# Initialize AHT20 humidity + temperature sensor
aht20 = AHTx0(i2c)

# Try initializing the INA226 current sensor with proper configuration
try:
    ina = INA226(address=0x40, shunt_ohms=0.352)  # Create INA226 object
    ina.configure(
        avg_mode=INA226.AVG_4BIT,  # Set averaging mode for noise reduction
        bus_ct=INA226.VCT_1100us_BIT,  # Set bus voltage conversion time
        shunt_ct=INA226.VCT_1100us_BIT  # Set shunt voltage conversion time
    )
except Exception as e:
    print("INA226 init/config error:", e)  # Print error if initialization fails
    ina = None  # Set INA226 object to None to prevent further crashes
    
    

#%%%%%%%%%%%%%%%%%%%%%%%%%%%%%  GPIO (Relays) Setup   %%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%

# GPIO pin numbers connected to relays
Relay = [5, 6, 13]

# Initialize relays as OutputDevice objects, active-high, initially ON (inactive)
relays = [OutputDevice(pin, active_high=True, initial_value=True) for pin in Relay]

# Initialize dictionary to track if manual override has been triggered for each relay
manual_override = {gpio: False for gpio in Relay}

%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%  Helper Methods   %%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%

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
            


#%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%% Logging for debugging   %%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%

logging.basicConfig(filename='/home/pi/bms.log', level=logging.INFO, format='%(asctime)s - %(message)s')
logging.info("Battery monitoring started")


#%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%% Main loop processing   %%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%
def main_loop():
    last_send_time = time()  # Store the time of the last data upload

    # Define threshold voltage levels
    LOW_THRESHOLD = 13.2   # Voltage below this turns the charger ON
    HIGH_THRESHOLD = 14.3  # Voltage above this turns the charger OFF

    charger_on = False  # Track current state of the charger

    while True:
        # Read humidity and temperature from sensors
        humidity, temperature = read_aht_sensor()
        
        # Read bus voltage from INA226 power monitor
        bus_voltage = read_ina_sensor()
        
        # Check if relay 6 is ON (manual override switch)
        if relays[Relay.index(6)].value:  # Relay is active-high
            manual_override[5] = False     # Auto-control relay 5 (charger)
            charger_on = False             # Charger should be OFF in auto
        else:
            manual_override[5] = True      # Manual control active
            charger_on = True              # Charger will be managed manually

        # Auto-control logic when no manual override
        if not manual_override[5]:
            if bus_voltage is not None:  # Ensure sensor reading is valid
                if bus_voltage < LOW_THRESHOLD and not charger_on:
                    relays[Relay.index(5)].off()  # Turn ON charger (relay active-low)
                    charger_on = True
                    # logging.info("Battery voltage low. Charger ON.")
                elif bus_voltage > HIGH_THRESHOLD and charger_on:
                    relays[Relay.index(5)].on()   # Turn OFF charger
                    charger_on = False
                    # logging.info("Battery voltage high. Charger OFF.")

        # Upload data every `timer_delay` seconds
        if time() - last_send_time > timer_delay:
            last_send_time = time()  # Reset timer
            timestamp = get_timestamp()  # Get current timestamp

            # Upload if all sensor readings are valid
            if humidity and temperature and bus_voltage:
                data = {
                    "temperature": temperature,
                    "humidity": humidity,
                    "voltage": bus_voltage,
                    "timestamp": timestamp,
                }

                # Upload sensor data to Firebase under user path
                ref_path = f"/UsersData/{USER_UID}/readings/{timestamp}"
                db.reference(ref_path).set(data)

                print("Data uploaded:", data)

                # Upload relay states
                update_relay_states()

                # Additional real-time feedback in terminal
                print(f"Bus Voltage: {ina.voltage():.2f} V")
                print(f"Shunt Voltage: {ina.shunt_voltage():.2f} V")
                print(f"Current: {ina.current():.2f} A")
                print(f"Power: {ina.power():.2f} W")
            else:
                print("Sensor read failed")
        
        sleep(1)  # Small delay to reduce CPU usage

# Entry point of the program
if __name__ == "__main__":
    # Start Firebase listener in a separate thread
    stream_thread = threading.Thread(target=init_firebase_stream)
    stream_thread.daemon = True  # Ensure thread exits when main program exits
    stream_thread.start()

    print("Listening for Firebase changes...")

    # Run main monitoring loop continuously
    while True:
        try:
            main_loop()  # Launch main battery management logic
        except KeyboardInterrupt:
            # Handle Ctrl+C gracefully
            print("Exiting by user...")
            break
        except Exception as e:
            # Catch other errors and attempt restart
            print(f"Error in main loop: {e}")
            print("Restarting main_loop in 5 seconds...")
            time.sleep(5)


