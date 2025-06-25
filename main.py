import numpy as np
from record3d import Record3DStream
import cv2
from threading import Event
import time
import math
import json
import asyncio
import websockets
from concurrent.futures import ThreadPoolExecutor
import threading
import queue

THRESHOLD_ZONES = {
    "zone1": (0.350, 0.325),  # Zone 1: 350mm to 325mm
    "zone2": (0.325, 0.300),  # Zone 2: 325mm to 300mm
    "zone3": (0.300, 0.275),  # Zone 3: 300mm to 275mm
    "zone4": (0.275, 0.250),  # Zone 4: 275mm to 250mm
    "zone5": (0.250, 0.225),  # Zone 5: 250mm to 225mm
    "zone6": (0.225, 0.200),  # Zone 6: 225mm to 200mm
}

ZONE_KEY_MAP = {
    "zone1": "KEY_6",
    "zone2": "KEY_5",
    "zone3": "KEY_4",
    "zone4": "KEY_3",
    "zone5": "KEY_2",
    "zone6": "KEY_1",
}

WEBSOCKET_URL = "ws://0.0.0.0:8000/ws"  # Example WebSocket URL

JSON_WS_PACKET_TEMPLATE = {"device_id": "chuhands-lidar", "timestamp": 0, "events": []}


def key_press_template(key):
    """
    Template for key press event in JSON format matching Rust InputEvent structure
    """
    return {"Keyboard": {"KeyPress": {"key": key}}}


def key_release_template(key):
    """
    Template for key release event in JSON format matching Rust InputEvent structure
    """
    return {"Keyboard": {"KeyRelease": {"key": key}}}


def create_timestamp_epoch_ms():
    """
    Create a timestamp in milliseconds since the epoch, as int
    """
    return int(time.time() * 1000)


def create_input_packet(events, device_id="chuhands-lidar"):
    """
    Create an InputEventPacket with the given events
    """
    packet = JSON_WS_PACKET_TEMPLATE.copy()
    packet["device_id"] = device_id
    packet["timestamp"] = create_timestamp_epoch_ms()
    packet["events"] = events
    return packet


def handle_zone_entry(zone_name, depth_value):
    """
    Handle the entry into a specific zone based on the depth value.
    Generate key press event based on zone.
    """
    print(f"Entered {zone_name} with depth: {depth_value:.3f}m")

    if zone_name in ZONE_KEY_MAP:
        key = ZONE_KEY_MAP[zone_name]
        key_press_event = key_press_template(key)
        packet = create_input_packet([key_press_event])
        print(f"Generated KeyPress event: {json.dumps(packet, indent=2)}")

        # Send to WebSocket
        send_packet_to_websocket(packet)


def handle_zone_exit(zone_name, depth_value):
    """
    Handle the exit from a specific zone based on the depth value.
    Generate key release event based on zone.
    """
    if depth_value is not None:
        print(f"Exited {zone_name} with depth: {depth_value:.3f}m")
    else:
        print(f"Exited {zone_name} (no object detected)")

    if zone_name in ZONE_KEY_MAP:
        key = ZONE_KEY_MAP[zone_name]
        key_release_event = key_release_template(key)
        packet = create_input_packet([key_release_event])
        print(f"Generated KeyRelease event: {json.dumps(packet, indent=2)}")

        # Send to WebSocket
        send_packet_to_websocket(packet)


class WebSocketManager:
    def __init__(self, url):
        self.url = url
        self.websocket = None
        self.message_queue = queue.Queue()
        self.running = False
        self.loop = None
        self.thread = None

    def start(self):
        """Start the WebSocket manager in a background thread"""
        self.running = True
        self.thread = threading.Thread(target=self._run_websocket_thread, daemon=True)
        self.thread.start()

    def stop(self):
        """Stop the WebSocket manager"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=1.0)

    def send_message(self, message):
        """Add a message to the queue to be sent"""
        if self.running:
            self.message_queue.put(message)

    def _run_websocket_thread(self):
        """Run the WebSocket event loop in a separate thread"""
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._websocket_handler())
        except Exception as e:
            print(f"WebSocket thread error: {e}")
        finally:
            self.loop.close()

    async def _websocket_handler(self):
        """Handle WebSocket connection and message sending"""
        while self.running:
            try:
                async with websockets.connect(self.url) as websocket:
                    self.websocket = websocket
                    print(f"Connected to WebSocket: {self.url}")

                    while self.running:
                        try:
                            # Check for messages to send (non-blocking)
                            try:
                                message = self.message_queue.get_nowait()
                                await websocket.send(message)
                                print(f"Sent to WebSocket: {message}")
                            except queue.Empty:
                                pass

                            # Small delay to prevent busy waiting
                            await asyncio.sleep(0.01)

                        except websockets.exceptions.ConnectionClosed:
                            print("WebSocket connection closed, will reconnect...")
                            break
                        except Exception as e:
                            print(f"Error sending message: {e}")

            except Exception as e:
                print(f"WebSocket connection error: {e}")
                if self.running:
                    print("Retrying connection in 2 seconds...")
                    await asyncio.sleep(2)


# Global WebSocket manager instance
ws_manager = WebSocketManager(WEBSOCKET_URL)


def send_packet_to_websocket(packet):
    """
    Send a packet to WebSocket using the persistent connection
    """
    message = json.dumps(packet)
    ws_manager.send_message(message)


class ClosestDepthApp:
    def __init__(self):
        self.event = Event()
        self.session = None
        self.DEVICE_TYPE__TRUEDEPTH = 0
        self.DEVICE_TYPE__LIDAR = 1
        self.running = True
        self.current_zone = None  # Track the current zone

    def on_new_frame(self):
        """
        This method is called from non-main thread, therefore cannot be used for presenting UI.
        """
        self.event.set()  # Notify the main thread to stop waiting and process new frame.

    def on_stream_stopped(self):
        print("Stream stopped")
        self.running = False

    def connect_to_device(self, dev_idx):
        print("Searching for devices")
        devs = Record3DStream.get_connected_devices()
        print("{} device(s) found".format(len(devs)))
        for dev in devs:
            print("\tID: {}\n\tUDID: {}\n".format(dev.product_id, dev.udid))

        if len(devs) <= dev_idx:
            raise RuntimeError(
                "Cannot connect to device #{}, try different index.".format(dev_idx)
            )

        dev = devs[dev_idx]
        self.session = Record3DStream()
        self.session.on_new_frame = self.on_new_frame
        self.session.on_stream_stopped = self.on_stream_stopped
        self.session.connect(dev)  # Initiate connection and start capturing

    def handle_zone_change(self, new_zone, depth_value):
        """
        Handle zone transitions by calling appropriate handler functions
        """
        if self.current_zone != new_zone:
            # Exit the previous zone if there was one
            if self.current_zone is not None:
                handle_zone_exit(self.current_zone, depth_value)

            # Enter the new zone if there is one
            if new_zone is not None:
                handle_zone_entry(new_zone, depth_value)

            # Update current zone
            self.current_zone = new_zone

    def get_zone_for_depth(self, depth_value):
        """
        Determine which zone the depth value falls into
        Anything closer than 200mm gets bounded to zone 6
        """
        if depth_value is None:
            return None

        # If closer than 200mm, assign to zone 6
        if depth_value < 0.200:
            return "zone6"

        for zone_name, (min_depth, max_depth) in THRESHOLD_ZONES.items():
            if min_depth >= depth_value >= max_depth:
                return zone_name
        return None

    def find_closest_depth(self, depth_frame):
        """
        Find the closest (minimum) depth value in the frame, excluding zeros
        and excluding the last 55 pixels in Y direction (bottom of frame)
        """
        # Create a zone excluding the last 55 pixels in Y direction
        zone_depth = depth_frame[:-70, :]  # Exclude bottom 55 rows

        # Filter out zero values (invalid depth readings)
        valid_depth = zone_depth[zone_depth > 0]

        if len(valid_depth) > 0:
            closest_depth = np.min(valid_depth)
            # Find the position of the closest depth within the zone
            closest_pos = np.unravel_index(
                np.argmin(np.where(zone_depth > 0, zone_depth, np.inf)),
                zone_depth.shape,
            )
            return closest_depth, closest_pos
        else:
            return None, None

    def start_processing_stream(self):
        print("Starting depth processing... Press 'q' to quit")

        try:
            while self.running:
                self.event.wait(timeout=1.0)  # Wait for new frame with timeout

                if not self.running:
                    break

                # Copy the newly arrived depth frame
                depth = self.session.get_depth_frame()

                if depth is None:
                    continue

                # Postprocess if needed (flip for TrueDepth cameras)
                if self.session.get_device_type() == self.DEVICE_TYPE__TRUEDEPTH:
                    depth = cv2.flip(depth, 1)

                # Find the closest depth value
                closest_depth, closest_pos = self.find_closest_depth(depth)

                if closest_depth is not None:
                    zone = self.get_zone_for_depth(closest_depth)

                    # Handle zone changes
                    self.handle_zone_change(zone, closest_depth)

                    zone_info = f" Zone: {zone}" if zone else ""
                    print(
                        f"Closest depth: {closest_depth:.3f}m at position {closest_pos}{zone_info}"
                    )
                else:
                    # Handle case when no depth is detected (exit current zone)
                    if self.current_zone is not None:
                        handle_zone_exit(self.current_zone, None)
                        self.current_zone = None
                    print("No valid depth data found")

                # Optional: Display the depth frame with closest point highlighted
                depth_display = cv2.normalize(
                    depth, None, 0, 255, cv2.NORM_MINMAX, cv2.CV_8U
                )
                depth_colored = cv2.applyColorMap(depth_display, cv2.COLORMAP_JET)

                # Draw a line to show the excluded zone (last 55 pixels in Y)
                if depth.shape[0] > 55:
                    zone_line_y = depth.shape[0] - 55
                    cv2.line(
                        depth_colored,
                        (0, zone_line_y),
                        (depth.shape[1], zone_line_y),
                        (0, 0, 255),
                        2,
                    )
                    cv2.putText(
                        depth_colored,
                        "Excluded Zone",
                        (10, zone_line_y + 20),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.6,
                        (0, 0, 255),
                        2,
                    )

                # Mark the closest point if found
                if closest_pos is not None:
                    zone = self.get_zone_for_depth(closest_depth)
                    cv2.circle(
                        depth_colored,
                        (closest_pos[1], closest_pos[0]),
                        10,
                        (255, 255, 255),
                        2,
                    )
                    # Show depth and zone information
                    zone_text = f"Zone: {zone}" if zone else "No Zone"
                    cv2.putText(
                        depth_colored,
                        f"{closest_depth:.3f}m",
                        (closest_pos[1] + 15, closest_pos[0] - 15),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.7,
                        (255, 255, 255),
                        2,
                    )
                    cv2.putText(
                        depth_colored,
                        zone_text,
                        (closest_pos[1] + 15, closest_pos[0] + 5),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.6,
                        (255, 255, 255),
                        2,
                    )

                cv2.imshow("Depth with Closest Point", depth_colored)

                # Check for quit key
                key = cv2.waitKey(1) & 0xFF
                if key == ord("q"):
                    self.running = False
                    break

                self.event.clear()

        except KeyboardInterrupt:
            print("\nStopping...")
            self.running = False
        finally:
            cv2.destroyAllWindows()
            if self.session:
                self.session.stop_stream()


def main():
    # Start the WebSocket manager
    ws_manager.start()

    app = ClosestDepthApp()
    try:
        app.connect_to_device(dev_idx=0)
        app.start_processing_stream()
    except RuntimeError as e:
        print(f"Error: {e}")
    except Exception as e:
        print(f"Unexpected error: {e}")
    finally:
        # Stop the WebSocket manager when done
        ws_manager.stop()


if __name__ == "__main__":
    main()
