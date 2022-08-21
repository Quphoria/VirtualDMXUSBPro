import json, time, threading
from threading import Lock
from stupidArtnet import StupidArtnetServer
import serial

def load_config():
    config = {
        "COM Port": "COM17",
        "Baud Rate": 115200,
        "ArtNet Universe": 0,
        "ArtNet Subnet": 0,
        "ArtNet Net": 0
    }

    try:
        with open("config.json") as f:
            temp_config = json.load(f)
        assert type(config) == type(temp_config), "Invalid Config"
        config_invalid = False
        for k in config:
            if k not in temp_config:
                print(k, "missing in config.json")
                config_invalid = True
                continue
            if type(config[k]) != type(temp_config[k]):
                print(k, "incorrect type in config.json")
                config_invalid = True
                continue
            config[k] = temp_config[k]
        assert not config_invalid, "Invalid Config Value"
    except Exception as ex:
        print("Error loading config:", ex)
        with open("config.json", "w") as f:
            json.dump(config, f, indent=4)
        print("Config overwritten")
    return config

ser = None
serial_dmx_input = True
serial_dmx_on_change = False

DMX_SC = 0
LBL_GET_PARAMS = 3
LBL_SET_PARAMS = 4
LBL_RECV_DMX = 5
LBL_SEND_DMX = 6
LBL_RECV_DMX_ON_CHANGE = 8
LBL_RECV_DMX_CHANGE = 9
LBL_GET_SERIAL_NUMBER = 10
FIRMWARE_VERSION_MSB = 1
FIRMWARE_VERSION_LSB = 44
SERIAL_NUMBER = 0x1337C0DE

def serial_init(config):
    global ser
    ## idk windows likes \\.\ prefixed to com port paths
    ser = serial.Serial(config.get("COM Port"), config.get("Baud Rate", 115200), timeout=0.5)

serial_send_lock = Lock()
def serial_send(label, data):
    if ser is None:
        return
    with serial_send_lock:
        msg_len = len(data)
        msg = [0x7E, label, msg_len & 0xff, (msg_len >> 8) & 0xff] + data + [0xE7]
        ser.write(bytes(msg))

fake_param_break_time = 9
fake_param_mab_time = 1
fake_param_output_rate = 0
fake_param_user_config = [0] * 508

def handle_serial_message(label, msg):
    global fake_param_break_time, fake_param_mab_time, fake_param_output_rate, fake_param_user_config
    if label != LBL_GET_PARAMS and label != LBL_SEND_DMX:
        serial_dmx_input = True
    if label == LBL_GET_PARAMS:
        user_config_size = 0
        if len(msg) == 2:
            user_config_size = msg[0] + (msg[1] << 8)
        serial_send(LBL_GET_PARAMS, [
            FIRMWARE_VERSION_LSB,
            FIRMWARE_VERSION_MSB,
            fake_param_break_time,
            fake_param_mab_time,
            fake_param_output_rate]
            + fake_param_user_config[:user_config_size])
    elif label == LBL_SET_PARAMS:
        if len(msg) >= 5:
            user_config_size = msg[0] + (msg[1] << 8)
            fake_param_break_time = msg[2]
            fake_param_mab_time = msg[3]
            fake_param_output_rate = msg[4]
            if len(msg) - 5 == user_config_size:
                for i in range(user_config_size):
                    fake_param_user_config[i] = msg[5+i]
    elif label == LBL_SEND_DMX:
        serial_dmx_input = False
    elif label == LBL_RECV_DMX_ON_CHANGE:
        if len(msg) == 1:
            serial_dmx_on_change = msg[0] == 1
    elif label == LBL_GET_SERIAL_NUMBER:
        serial_send(LBL_GET_SERIAL_NUMBER, [
            SERIAL_NUMBER & 0xff,
            (SERIAL_NUMBER >> 8) & 0xff,
            (SERIAL_NUMBER >> 16) & 0xff,
            (SERIAL_NUMBER >> 24) & 0xff
        ])

def serial_read_byte():
    assert ser is not None, "Serial port uninitialized"
    while True:
        # I have a timeout to KeyboardInterrupt exceptions still work
        # So you can press Ctrl-C to exit program
        try:
            d = ser.read(1)
            if d:
                return d[0]
        except serial.SerialTimeoutException:
            pass

def serial_read():
    while ser is not None:
        # wait for start
        d = serial_read_byte()
        while d != 0x7E:
            d = serial_read_byte()
        label = serial_read_byte()
        data_len = serial_read_byte()
        data_len |= serial_read_byte() << 8
        msg = []
        for i in range(data_len):
            msg.append(serial_read_byte())
        d = serial_read_byte()
        # if next byte is end message, handle the message
        if d == 0xE7:
            handle_serial_message(label, msg)
            continue
        # if it isn't, try to resync by waiting for end of message byte
        while d != 0xE7:
            d = serial_read_byte()

last_artnet = 0
has_artnet = False
class ArtNetLostMessageThread(threading.Thread):
    def __init__(self):
        threading.Thread.__init__(self)
        self.exit = False
    def run(self):
        global has_artnet
        while not self.exit:
            time.sleep(1)
            if time.time() - last_artnet > 3 and has_artnet: # 3 seconds since last artnet
                print("ArtNet Connection Lost")
                has_artnet = False

last_dmx = [0]*512
def artnet_receive(data):
    global last_artnet, has_artnet
    if time.time() - last_artnet > 3: # 3 seconds since last artnet
        print("ArtNet Connection Established")
        has_artnet = True
    last_artnet = time.time()
    if ser is None or not serial_dmx_input:
        return
    if not serial_dmx_on_change:
        recv_status = 0
        serial_send(LBL_RECV_DMX, [recv_status, DMX_SC] + data)
    else:
        i = 1
        while i < len(data)+1: # add virtual start code byte
            if last_dmx[i-1] != data[i-1]:
                start_byte = i % 8
                byte_offset = i - start_byte
                changed_bit_array = [0] * 5
                changed_data = []
                for x in range(i, min(start_byte+40, len(data)+1)):
                    if last_dmx[i-1] != data[i-1]:
                        changed_data.append(data[i-1])
                        last_dmx[i-1] = data[i-1]
                        changed_bit_array[byte_offset >> 3] |= 1 << (byte_offset & 0b111)
                    byte_offset += 1
                serial_send(LBL_RECV_DMX_CHANGE, [start_byte // 8] + changed_bit_array + changed_data)
                i = min(start_byte+40, len(data)+1) # skip checked bytes
            else:
                i += 1

artnet_server = None

def start_artnet_server(config):
    global artnet_server
    artnet_server = StupidArtnetServer()
    artnet_server.register_listener(
        universe=config.get("ArtNet Universe", 0),
        sub=config.get("ArtNet Subnet"),
        net=config.get("ArtNet Net", 0),
        callback_function=artnet_receive)

artnet_lost_thread = ArtNetLostMessageThread()

def shutdown():
    global artnet_server
    del artnet_server
    if ser:
        ser.close()
    artnet_lost_thread.exit = True
    artnet_lost_thread.join()

if __name__ == "__main__":
    try:
        config = load_config()
        serial_init(config)
        start_artnet_server(config)
        artnet_lost_thread.start()
        print("Ready to recieve artnet and serial commands!")
        serial_read()
    except KeyboardInterrupt:
        pass
    except Exception as ex:
        shutdown()
        raise ex
    shutdown()
