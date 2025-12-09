#!/usr/bin/env python
"""
Analyzer Connector
------------------

Reads results from a lab analyzer via RS-232 (serial) and sends them
to Nutryah HIMS / LIS backend:

    POST /api/lis/device-results
    Header: X-Device-Api-Key

Author: Nutryah HIMS
"""

from __future__ import annotations

import os
import sys
import time
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any
from analyzer_parsers import parse_message_by_protocol
import serial  # pip install pyserial
import requests  # pip install requests
from dotenv import load_dotenv  # pip install python-dotenv

# -------------------------------------------------------------------
#  Load configuration
# -------------------------------------------------------------------

# .env (same folder) example:
#
# BACKEND_BASE_URL=https://hims.example.com
# DEVICE_CODE=CBC1
# DEVICE_API_KEY=super-secret-device-key
# SERIAL_PORT=COM3
# SERIAL_BAUDRATE=9600
# SERIAL_BYTESIZE=8
# SERIAL_PARITY=N
# SERIAL_STOPBITS=1
# SERIAL_TIMEOUT=5

load_dotenv()

BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL", "http://127.0.0.1:8000")
DEVICE_CODE = os.getenv("DEVICE_CODE", "CBC1")
DEVICE_API_KEY = os.getenv("DEVICE_API_KEY", "changeme-device-key")

SERIAL_PORT = os.getenv("SERIAL_PORT", "COM3")
SERIAL_BAUDRATE = int(os.getenv("SERIAL_BAUDRATE", "9600"))
SERIAL_BYTESIZE = int(os.getenv("SERIAL_BYTESIZE", "8"))
SERIAL_PARITY = os.getenv("SERIAL_PARITY", "N")  # N, E, O, M, S
SERIAL_STOPBITS = int(os.getenv("SERIAL_STOPBITS", "1"))
SERIAL_TIMEOUT = float(os.getenv("SERIAL_TIMEOUT", "5"))

POST_URL = f"{BACKEND_BASE_URL.rstrip('/')}/api/lis/device-results"
LOG_FILE = os.getenv("LOG_FILE", "analyzer_connector.log")


# -------------------------------------------------------------------
#  Logging setup
# -------------------------------------------------------------------

logger = logging.getLogger("analyzer-connector")
logger.setLevel(logging.INFO)

# Console handler
ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.INFO)
ch_formatter = logging.Formatter(
    "[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
)
ch.setFormatter(ch_formatter)
logger.addHandler(ch)

# File handler
fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
fh.setLevel(logging.INFO)
fh_formatter = logging.Formatter(
    "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
fh.setFormatter(fh_formatter)
logger.addHandler(fh)


# -------------------------------------------------------------------
#  Serial helpers
# -------------------------------------------------------------------

def open_serial_port() -> serial.Serial:
    """Open and return the configured serial port."""
    parity_map = {
        "N": serial.PARITY_NONE,
        "E": serial.PARITY_EVEN,
        "O": serial.PARITY_ODD,
        "M": serial.PARITY_MARK,
        "S": serial.PARITY_SPACE,
    }
    stopbits_map = {
        1: serial.STOPBITS_ONE,
        1.5: serial.STOPBITS_ONE_POINT_FIVE,
        2: serial.STOPBITS_TWO,
    }

    logger.info(
        "Opening serial port %s (baud=%s, bytesize=%s, parity=%s, stopbits=%s, timeout=%s)",
        SERIAL_PORT,
        SERIAL_BAUDRATE,
        SERIAL_BYTESIZE,
        SERIAL_PARITY,
        SERIAL_STOPBITS,
        SERIAL_TIMEOUT,
    )

    ser = serial.Serial(
        port=SERIAL_PORT,
        baudrate=SERIAL_BAUDRATE,
        bytesize=SERIAL_BYTESIZE,
        parity=parity_map.get(SERIAL_PARITY.upper(), serial.PARITY_NONE),
        stopbits=stopbits_map.get(SERIAL_STOPBITS, serial.STOPBITS_ONE),
        timeout=SERIAL_TIMEOUT,
    )
    return ser


def read_raw_message(ser: serial.Serial) -> str | None:
    """
    Read a raw message from the analyzer.

    This function is intentionally simple:
    - Reads until timeout.
    - Joins all received lines into a single string.

    For many ASTM devices, you might want to:
    - Listen until EOT (0x04).
    - Or accumulate frames starting with STX and ending with ETX + checksum.

    This is device-specific; you can enhance as needed.
    """
    try:
        logger.info("Waiting for data from analyzer...")
        lines: list[str] = []
        # Readmultiple lines until timeout or no more input
        while True:
            raw = ser.readline()  # reads until \n or timeout
            if not raw:
                break
            try:
                line = raw.decode("ascii", errors="ignore").strip()
            except UnicodeDecodeError:
                line = raw.decode("latin-1", errors="ignore").strip()

            if not line:
                continue

            logger.info("Received line: %s", line)
            lines.append(line)

        if not lines:
            return None

        message = "\n".join(lines)
        logger.info("Complete message received (%d lines).", len(lines))
        return message

    except serial.SerialException as e:
        logger.error("Serial error while reading: %s", e)
        return None


# -------------------------------------------------------------------
#  Parsing raw message → DeviceResultBatchIn payload
# -------------------------------------------------------------------
DEVICE_PROTOCOL = os.getenv("DEVICE_PROTOCOL", "csv")  # csv | astm | hl7


def parse_raw_message_to_results(raw: str) -> List[Dict[str, Any]]:
    """
    Wrapper that uses analyzer_parsers module based on DEVICE_PROTOCOL.
    """
    return parse_message_by_protocol(raw, DEVICE_PROTOCOL)



def build_device_result_batch(raw: str) -> Dict[str, Any]:
    """
    Build payload matching DeviceResultBatchIn schema:

    {
      "device_code": "...",
      "results": [ { ... DeviceResultItemIn ... } ],
      "raw_payload": "original raw message"
    }
    """
    result_items = parse_raw_message_to_results(raw)
    payload = {
        "device_code": DEVICE_CODE,
        "results": result_items,
        "raw_payload": raw,
    }
    return payload


# -------------------------------------------------------------------
#  Sending to backend
# -------------------------------------------------------------------

def post_results_to_backend(payload: Dict[str, Any]) -> bool:
    """
    Send the DeviceResultBatchIn payload to Nutryah HIMS backend.

    Returns True on success, False otherwise.
    """
    headers = {
        "Content-Type": "application/json",
        "X-Device-Api-Key": DEVICE_API_KEY,
    }

    try:
        logger.info("Posting %d result(s) to backend: %s", len(payload["results"]), POST_URL)
        resp = requests.post(POST_URL, json=payload, headers=headers, timeout=15)

        if resp.status_code == 201:
            logger.info("Results posted successfully. Response: %s", resp.text[:200])
            return True
        else:
            logger.error(
                "Backend returned status %s. Response: %s",
                resp.status_code,
                resp.text[:500],
            )
            return False

    except requests.RequestException as e:
        logger.error("Error posting to backend: %s", e)
        return False


# -------------------------------------------------------------------
#  Main loop
# -------------------------------------------------------------------

def main_loop() -> None:
    logger.info("Starting Analyzer Connector for device '%s'", DEVICE_CODE)
    logger.info("Backend URL: %s", BACKEND_BASE_URL)

    ser: serial.Serial | None = None

    while True:
        try:
            if ser is None or not ser.is_open:
                ser = open_serial_port()
                logger.info("Serial port opened successfully.")

            raw_msg = read_raw_message(ser)
            if raw_msg:
                logger.info("Raw message:\n%s", raw_msg)
                payload = build_device_result_batch(raw_msg)

                if not payload["results"]:
                    logger.warning("No results to send. Skipping POST.")
                else:
                    ok = post_results_to_backend(payload)
                    if not ok:
                        logger.error("Failed to send results to backend. Will retry on next message.")

            # Sleep a bit to avoid busy loop; adjust based on device
            time.sleep(1.0)

        except serial.SerialException as e:
            logger.error("SerialException: %s. Will retry in 10 seconds.", e)
            if ser and ser.is_open:
                try:
                    ser.close()
                except Exception:
                    pass
            ser = None
            time.sleep(10)

        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received. Exiting...")
            break

        except Exception as e:
            # Catch-all to prevent connector crash
            logger.exception("Unexpected error in main loop: %s", e)
            time.sleep(5)

    # Cleanup
    if ser and ser.is_open:
        try:
            ser.close()
        except Exception:
            pass
    logger.info("Analyzer Connector stopped.")


if __name__ == "__main__":
    main_loop()
