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
from threading import Timer

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


def get_zone_number(zone_name):
    """Convert zone name to number for ordering (zone1=1, zone6=6)"""
    if zone_name is None:
        return None
    return int(zone_name.replace("zone", ""))


def get_zones_between(start_zone, end_zone):
    """Get all zones between start and end (inclusive)"""
    if start_zone is None or end_zone is None:
        return []

    start_num = get_zone_number(start_zone)
    end_num = get_zone_number(end_zone)

    if start_num == end_num:
        return [start_zone]

    # Determine direction
    if start_num < end_num:
        return [f"zone{i}" for i in range(start_num, end_num + 1)]
    else:
        return [f"zone{i}" for i in range(start_num, end_num - 1, -1)]


def send_key_press(zone_name):
    """Send a key press event for a specific zone"""
    if zone_name in ZONE_KEY_MAP:
        key = ZONE_KEY_MAP[zone_name]
        key_press_event = key_press_template(key)
        packet = create_input_packet([key_press_event])
        print(f"KeyPress: {key} ({zone_name})")
        send_packet_to_websocket(packet)


def send_key_release(zone_name):
    """Send a key release event for a specific zone"""
    if zone_name in ZONE_KEY_MAP:
        key = ZONE_KEY_MAP[zone_name]
        key_release_event = key_release_template(key)
        packet = create_input_packet([key_release_event])
        print(f"KeyRelease: {key} ({zone_name})")
        send_packet_to_websocket(packet)


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
        # Remove verbose JSON printing for performance
        print(f"KeyPress: {key}")

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
        # Remove verbose JSON printing for performance
        print(f"KeyRelease: {key}")

        # Send to WebSocket
        send_packet_to_websocket(packet)


class WebSocketManager:
    def __init__(self, url, max_queue_size=10):
        self.url = url
        self.websocket = None
        self.message_queue = queue.Queue(maxsize=max_queue_size)
        self.running = False
        self.loop = None
        self.thread = None
        self.max_queue_size = max_queue_size

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
            try:
                # Try to put message without blocking
                self.message_queue.put_nowait(message)
            except queue.Full:
                # If queue is full, remove oldest message and add new one
                try:
                    self.message_queue.get_nowait()  # Remove oldest
                    self.message_queue.put_nowait(message)  # Add new
                    print("WebSocket queue full, dropped oldest message")
                except queue.Empty:
                    pass

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
                # Set connection timeout
                async with websockets.connect(
                    self.url,
                    ping_timeout=10,
                    close_timeout=5,
                    max_size=2**16,  # Smaller max message size
                ) as websocket:
                    self.websocket = websocket
                    print(f"Connected to WebSocket: {self.url}")

                    while self.running:
                        try:
                            # Check for messages to send with timeout
                            try:
                                message = self.message_queue.get(timeout=0.1)

                                # Send with timeout to prevent hanging
                                await asyncio.wait_for(
                                    websocket.send(message), timeout=1.0
                                )
                                # Reduce verbose logging for performance
                                # print(f"Sent to WebSocket: {message}")

                            except queue.Empty:
                                # Send ping to keep connection alive
                                await asyncio.wait_for(websocket.ping(), timeout=1.0)
                            except asyncio.TimeoutError:
                                print("WebSocket send timeout, reconnecting...")
                                break

                        except websockets.exceptions.ConnectionClosed:
                            print("WebSocket connection closed, will reconnect...")
                            break
                        except Exception as e:
                            print(f"Error in WebSocket loop: {e}")
                            break

            except Exception as e:
                print(f"WebSocket connection error: {e}")
                # Clear the queue to prevent buildup during reconnection
                while not self.message_queue.empty():
                    try:
                        self.message_queue.get_nowait()
                    except queue.Empty:
                        break
                print("Cleared message queue due to connection error")

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

        # Performance optimizations for minimal latency
        self.show_visualization = True  # Disable visualization by default for speed

        # Pre-compute zone boundaries for faster lookup
        self.zone_boundaries = [
            (min_depth, max_depth, zone_name)
            for zone_name, (min_depth, max_depth) in THRESHOLD_ZONES.items()
        ]

        # Depth processing optimizations
        self.last_depth_value = None
        self.zone_change_threshold = 0.005  # 5mm threshold to prevent jitter

        # CHUNITHM-style input handling
        self.active_zones = set()  # Track all currently pressed zones
        self.zone_release_timers = {}  # Track delayed releases
        self.release_delay_ms = 10  # 10ms delay between releases

        # Initialize WebSocket manager
        self.websocket_manager = None

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

    def handle_zone_change_optimized(self, new_zone, depth_value):
        """
        Optimized zone change handling with jitter prevention and CHUNITHM-style multi-key input
        """
        # Only process if zone actually changed and passes jitter threshold
        if self.current_zone != new_zone:
            if (
                self.last_depth_value is None
                or abs(depth_value - self.last_depth_value) > self.zone_change_threshold
            ):
                # Use CHUNITHM-style zone handling
                self.handle_zone_change_chunithm(new_zone)

                # Update last depth
                self.last_depth_value = depth_value

    def get_zone_for_depth(self, depth_value):
        """
        Determine which zone the depth value falls into (optimized version)
        Anything closer than 200mm gets bounded to zone 6
        """
        if depth_value is None:
            return None

        # If closer than 200mm, assign to zone 6
        if depth_value < 0.200:
            return "zone6"

        # Use pre-computed boundaries for faster lookup
        for min_depth, max_depth, zone_name in self.zone_boundaries:
            if min_depth >= depth_value >= max_depth:
                return zone_name
        return None

    def find_closest_depth_optimized(self, depth_frame):
        """
        Ultra-fast depth processing - only find minimum, no position tracking
        """
        # Create a zone excluding the last 70 pixels in Y direction
        zone_depth = depth_frame[:-70, :]

        # Fast minimum finding with numpy
        valid_depth = zone_depth[zone_depth > 0]
        if len(valid_depth) == 0:
            return None

        return np.min(valid_depth)

    def start_processing_stream(self):
        print("Starting LOW-LATENCY depth processing...")
        print("Commands: 'q'=quit, 'v'=toggle visualization")

        try:
            while self.running:
                self.event.wait(timeout=1.0)  # Wait for new frame with timeout

                if not self.running:
                    break

                # Get depth frame immediately - no copying overhead
                depth = self.session.get_depth_frame()
                if depth is None:
                    continue

                # Skip TrueDepth flip for LiDAR devices (optimization)
                if self.session.get_device_type() == self.DEVICE_TYPE__TRUEDEPTH:
                    depth = cv2.flip(depth, 1)

                # Ultra-fast depth processing - only get minimum value
                closest_depth = self.find_closest_depth_optimized(depth)

                if closest_depth is not None:
                    zone = self.get_zone_for_depth(closest_depth)
                    # Use optimized zone change handler
                    self.handle_zone_change_optimized(zone, closest_depth)
                else:
                    # Handle case when no depth is detected (exit all zones)
                    self.handle_zone_change_optimized(None, None)

                # Minimal visualization processing (only when enabled)
                if self.show_visualization:
                    self._update_visualization_minimal(depth, closest_depth)

                # Minimal key processing
                key = cv2.waitKey(1) & 0xFF
                if key == ord("q"):
                    self.running = False
                    break
                elif key == ord("v"):
                    self.show_visualization = not self.show_visualization
                    print(
                        f"Visualization: {'ON' if self.show_visualization else 'OFF'}"
                    )

                self.event.clear()

        except KeyboardInterrupt:
            print("\nStopping...")
            self.running = False
        finally:
            if self.show_visualization:
                cv2.destroyAllWindows()
            if self.session:
                self.session.stop_stream()

    def _update_visualization_minimal(self, depth, closest_depth):
        """
        Minimal visualization for performance - only show essential info
        """
        # Simple depth visualization without expensive operations
        depth_display = cv2.normalize(depth, None, 0, 255, cv2.NORM_MINMAX, cv2.CV_8U)

        # Show zone info as text overlay only
        if closest_depth is not None:
            zone = self.get_zone_for_depth(closest_depth)
            zone_text = f"Zone: {zone} | Depth: {closest_depth:.3f}m"
            cv2.putText(
                depth_display,
                zone_text,
                (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (255, 255, 255),
                2,
            )

        cv2.imshow("Depth (Minimal)", depth_display)

    def handle_zone_change_chunithm(self, new_zone):
        """Handle zone changes with CHUNITHM-style multi-key input for rapid transitions"""
        old_zone = self.current_zone

        if old_zone == new_zone:
            return  # No change

        # Clear any existing release timers
        for timer in self.zone_release_timers.values():
            timer.cancel()
        self.zone_release_timers.clear()

        # If we were in a zone and now leaving
        if old_zone is not None and new_zone is None:
            # Release all active zones
            for zone in list(self.active_zones):
                send_key_release(zone)
                self.active_zones.discard(zone)
            print(f"Zone transition: {old_zone} -> None")

        # If we're entering a zone from nothing
        elif old_zone is None and new_zone is not None:
            # Press the new zone key
            send_key_press(new_zone)
            self.active_zones.add(new_zone)
            print(f"Zone transition: None -> {new_zone}")

        # If we're changing from one zone to another
        elif old_zone is not None and new_zone is not None:
            old_num = get_zone_number(old_zone)
            new_num = get_zone_number(new_zone)

            # Check if it's a rapid transition (skipping zones)
            if abs(new_num - old_num) > 1:
                print(f"Rapid zone transition detected: {old_zone} -> {new_zone}")

                # Get all zones between old and new (inclusive)
                zones_to_press = get_zones_between(old_zone, new_zone)

                # Press all intermediate keys immediately
                for zone in zones_to_press:
                    if (
                        zone not in self.active_zones
                    ):  # Don't re-press already active zones
                        send_key_press(zone)
                        self.active_zones.add(zone)

                # Schedule releases in reverse order with delay
                # Use the WebSocket manager's event loop for timed releases
                async def release_keys_with_delay():
                    # Release keys in reverse order (highest to lowest if moving up, lowest to highest if moving down)
                    zones_to_release = zones_to_press[:-1]  # All except the final zone
                    if old_num < new_num:
                        # Moving up: release from highest to lowest
                        zones_to_release.reverse()
                    # Moving down: already in correct order (highest to lowest)

                    for zone in zones_to_release:
                        await asyncio.sleep(0.01)  # 10ms delay
                        send_key_release(zone)
                        self.active_zones.discard(zone)

                # Schedule the release sequence on the WebSocket manager's event loop
                if self.websocket_manager and self.websocket_manager.loop:
                    asyncio.run_coroutine_threadsafe(
                        release_keys_with_delay(), self.websocket_manager.loop
                    )
                else:
                    # Fallback: use threading timer for compatibility
                    def delayed_release(zone, delay):
                        def release_action():
                            send_key_release(zone)
                            self.active_zones.discard(zone)

                        timer = Timer(delay, release_action)
                        timer.start()
                        return timer

                    zones_to_release = zones_to_press[:-1]
                    if old_num < new_num:
                        zones_to_release.reverse()

                    for i, zone in enumerate(zones_to_release):
                        timer = delayed_release(zone, (i + 1) * 0.01)  # 10ms intervals
                        self.zone_release_timers[zone] = timer

            else:
                # Normal adjacent zone transition
                if old_zone in self.active_zones:
                    send_key_release(old_zone)
                    self.active_zones.discard(old_zone)

                if new_zone not in self.active_zones:
                    send_key_press(new_zone)
                    self.active_zones.add(new_zone)

                print(f"Zone transition: {old_zone} -> {new_zone}")

        self.current_zone = new_zone


def main():
    # Start the WebSocket manager
    ws_manager.start()

    app = ClosestDepthApp()
    app.websocket_manager = ws_manager  # Assign the websocket manager to the app
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
