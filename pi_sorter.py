import cv2
import numpy as np
import time
import serial
import threading
from queue import Queue

# --- System & Timing Configuration ---
SERIAL_PORT = '/dev/ttyUSB0'   # Update if Pi enumerates as /dev/ttyACM0
BAUD_RATE = 115200
MODEL_PATH = 'mobilenet_v2_1.0_224_quant.tflite'
FRAME_WIDTH, FRAME_HEIGHT = 640, 480

# Calibration constants (tune via bench drops)
Y_DETECTION_LINE = 150      # Pixel row where classification + timing triggers
Y_NOZZLE_LINE    = 420      # Pixel row of physical nozzle center
INFERENCE_LAG_US = 28000    # Measured TFLite invoke() cost on Pi 3B (~28ms)
USB_LAG_US       = 2000     # USB-Serial round-trip overhead (~2ms)
VALVE_MECH_LAG_US = 8000   # Solenoid spring + seat travel (~8ms)

OBJECT_MAX_STALE_S = 0.5    # Prune tracked objects not seen within this window

# --- Serial Bridge ---
ser = None
try:
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1.0)
    time.sleep(2)  # Allow Arduino to finish boot/reset
    handshake = ser.readline().decode('utf-8', errors='ignore').strip()
    if handshake == 'MCU_READY':
        print("MCU_READY confirmed — serial bridge live.")
    else:
        print(f"Warning: unexpected MCU boot message: {handshake!r}")
except Exception as e:
    print(f"Warning: Serial skipped ({e}) — running in dry-fire mode.")

# --- Thread-Safe Inference Queue ---
# maxsize=1 ensures we never queue stale frames; new frames replace old ones
inference_queue = Queue(maxsize=1)
classification_result = {"class_id": 0, "lock": threading.Lock()}


def ml_inference_worker(model_path: str) -> None:
    """Runs TFLite inference on a dedicated thread to keep capture loop responsive."""
    try:
        import tflite_runtime.interpreter as tflite
    except ImportError:
        from tensorflow.lite.python import interpreter as tflite  # fallback for full TF install

    interpreter = tflite.Interpreter(model_path=model_path)
    interpreter.allocate_tensors()
    input_details  = interpreter.get_input_details()
    output_details = interpreter.get_output_details()
    _, h, w, _ = input_details[0]['shape']

    while True:
        frame_crop = inference_queue.get()
        if frame_crop is None:
            break  # Sentinel — clean shutdown

        resized = cv2.resize(frame_crop, (w, h))
        input_data = np.expand_dims(resized, axis=0)
        if input_details[0]['dtype'] == np.uint8:
            input_data = input_data.astype(np.uint8)
        else:
            input_data = (input_data.astype(np.float32) / 127.5) - 1.0

        interpreter.set_tensor(input_details[0]['index'], input_data)
        interpreter.invoke()
        output = interpreter.get_tensor(output_details[0]['index'])[0]

        with classification_result["lock"]:
            classification_result["class_id"] = int(np.argmax(output))
        inference_queue.task_done()


worker = threading.Thread(target=ml_inference_worker, args=(MODEL_PATH,), daemon=True)
worker.start()

# --- Video Capture ---
cap = cv2.VideoCapture(0)
cap.set(cv2.CAP_PROP_FRAME_WIDTH,  FRAME_WIDTH)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)

# object_history: {id: {'x', 'y', 't', 'v', 'triggered'}}
object_history: dict = {}
next_obj_id = 0

try:
    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break

        # Use monotonic clock — immune to NTP/system-clock jumps that corrupt dt
        current_time = time.monotonic()

        # --- Prune stale tracked objects ---
        stale = [oid for oid, data in object_history.items()
                 if current_time - data['t'] > OBJECT_MAX_STALE_S]
        for oid in stale:
            del object_history[oid]

        # --- Detect contours ---
        gray    = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (11, 11), 0)
        _, thresh = cv2.threshold(blurred, 60, 255, cv2.THRESH_BINARY_INV)
        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        matched_ids: set = set()

        for cnt in contours:
            if cv2.contourArea(cnt) < 400:
                continue
            M = cv2.moments(cnt)
            if M["m00"] == 0:
                continue
            cx = int(M["m10"] / M["m00"])
            cy = int(M["m01"] / M["m00"])

            # --- Match centroid to nearest existing track ---
            matched_id = None
            best_dist  = float('inf')
            for obj_id, data in object_history.items():
                if obj_id in matched_ids:
                    continue
                dx = abs(cx - data['x'])   # Bug fix: was abs(cx - cx) == 0 always
                dy = cy - data['y']
                if dx < 50 and 0 < dy < 100:
                    dist = dx + dy
                    if dist < best_dist:
                        best_dist  = dist
                        matched_id = obj_id

            if matched_id is not None:
                matched_ids.add(matched_id)
                prev = object_history[matched_id]
                dt   = current_time - prev['t']
                velocity_px_s = (cy - prev['y']) / dt if dt > 0 else prev['v']

                object_history[matched_id] = {
                    'x': cx, 'y': cy, 't': current_time,
                    'v': velocity_px_s,
                    'triggered': prev['triggered'],
                }

                # --- Trigger on detection line crossing ---
                if cy >= Y_DETECTION_LINE and not prev['triggered']:
                    object_history[matched_id]['triggered'] = True

                    # Hand off crop to ML worker; non-blocking (skip if busy)
                    if inference_queue.empty():
                        y0, y1 = max(0, cy - 50), min(FRAME_HEIGHT, cy + 50)
                        x0, x1 = max(0, cx - 50), min(FRAME_WIDTH,  cx + 50)
                        crop = frame[y0:y1, x0:x1]
                        if crop.size > 0:
                            inference_queue.put(crop)

                    if velocity_px_s > 0:
                        px_to_travel = Y_NOZZLE_LINE - cy
                        time_to_nozzle_us = int((px_to_travel / velocity_px_s) * 1_000_000)
                        delay_us = (time_to_nozzle_us
                                    - INFERENCE_LAG_US
                                    - USB_LAG_US
                                    - VALVE_MECH_LAG_US)

                        if delay_us > 0:
                            with classification_result["lock"]:
                                valve_target = 1 if classification_result["class_id"] == 1 else 2
                            command = f"V{valve_target},{delay_us}\n"
                            if ser:
                                ser.write(command.encode('utf-8'))
                            print(f"TX: {command.strip()}  vel={velocity_px_s:.1f}px/s  "
                                  f"travel={time_to_nozzle_us}us  delay={delay_us}us")
                        else:
                            print(f"WARN: delay_us={delay_us} — object too close or too slow; skipping.")
            else:
                # Register new track
                object_history[next_obj_id] = {
                    'x': cx, 'y': cy, 't': current_time,
                    'v': 0, 'triggered': False,
                }
                next_obj_id += 1

        # --- Debug overlay ---
        cv2.line(frame, (0, Y_DETECTION_LINE), (FRAME_WIDTH, Y_DETECTION_LINE), (255, 180, 0), 2)
        cv2.line(frame, (0, Y_NOZZLE_LINE),    (FRAME_WIDTH, Y_NOZZLE_LINE),    (0,   0, 255), 2)
        cv2.putText(frame, f"Tracks: {len(object_history)}", (10, 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 1)
        cv2.imshow("Aero-Sort Prototype Tracker", frame)

        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

finally:
    inference_queue.put(None)  # Signal worker thread to exit cleanly
    cap.release()
    cv2.destroyAllWindows()
    if ser:
        ser.close()
