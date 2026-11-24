# Extracted verbatim from home-assistant-awox awoxmeshlight/__init__.py
# (AwoxMeshLight._parseStatusResult), Copyright (c) 2020 Frans Saris - MIT.
# ruff: noqa
# type: ignore
import struct

# Constants copied from the same file.
C_GET_STATUS_RECEIVED = 0xdb
C_NOTIFICATION_RECEIVED = 0xdc


def parse_status_result(data):
    command = struct.unpack('B', data[7:8])[0]
    status = {}
    if command == C_GET_STATUS_RECEIVED:
        mode = struct.unpack('B', data[10:11])[0]
        mesh_id = (struct.unpack('B', data[4:5])[0] * 256) + struct.unpack('B', data[3:4])[0]
        white_brightness, white_temperature = struct.unpack('BB', data[11:13])
        color_brightness, red, green, blue = struct.unpack('BBBB', data[13:17])
        status = {
            'type': 'status',
            'mesh_id': mesh_id,
            'state': (mode & 1) == 1,
            'color_mode': ((mode >> 1) & 1) == 1,
            'transition_mode': ((mode >> 2) & 1) == 1,
            'red': red,
            'green': green,
            'blue': blue,
            'white_temperature': white_temperature,
            'white_brightness': white_brightness,
            'color_brightness': color_brightness,
        }

    if command == C_NOTIFICATION_RECEIVED:
        mesh_id = (struct.unpack('B', data[19:20])[0] * 256) + struct.unpack('B', data[10:11])[0]
        mode = struct.unpack('B', data[12:13])[0]
        white_brightness, white_temperature = struct.unpack('BB', data[13:15])
        color_brightness, red, green, blue = struct.unpack('BBBB', data[15:19])

        status = {
            'type': 'notification',
            'mesh_id': mesh_id,
            'state': (mode & 1) == 1,
            'color_mode': ((mode >> 1) & 1) == 1,
            'transition_mode': ((mode >> 2) & 1) == 1,
            'red': red,
            'green': green,
            'blue': blue,
            'white_temperature': white_temperature,
            'white_brightness': white_brightness,
            'color_brightness': color_brightness,
        }

    return status

