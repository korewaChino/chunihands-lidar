# chuhands-lidar

A LiDAR depth-to-zoned signals emitter thinggy

chuhands-lidar is a project that allows you to stream Record3D LiDAR data from an iOS device to 6 mappable zones,
intended for CHUNITHM-like games.

This program takes in depth data from the LiDAR sensor from an iOS device running Record3D,
and emits WebSocket messages to [Backflow](https://github.com/FyraLabs/backflow) to be used in a game.

## Prequisites

- An iOS device with a LiDAR sensor (iPhone 12 Pro, iPhone 13 Pro, iPhone 14 Pro, iPad Pro 2020 or later).
- [Record3D](https://apps.apple.com/us/app/record3d/id1506461980) installed on the iOS device.
- `uv` installed on your computer (for Python 3.11+).

## Usage

1. Install [Record3D](https://apps.apple.com/us/app/record3d/id1506461980) on your iOS device, at least buy the basic version for usbmuxd support.
2. Connect your iOS device to your computer via USB.
3. Clone this repository.
4. Run Backflow on your computer.
5. Run the Python script using `uv`

    ```bash
    uv run main.py
    ```

6. Enjoy CHUNITHM/UMIGURI without spending money on 6 IR sensors
