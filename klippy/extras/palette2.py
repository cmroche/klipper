# Palette 2 MMU support
#
# Copyright (C) 2021 Clifford Roche <clifford.roche@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import os
import threading
import time
import serial

try:
   from queue import Queue, Empty
except ImportError:
    from Queue import Queue, Empty

COMMAND_HEARTBEAT = "O99"

HEARTBEAT_SEND = 5
HEARTBEAT_TIMEOUT = 11

class Palette2:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object("gcode")
        self.gcode.register_command(
            "PALETTE_CONNECT", self.cmd_Connect, desc=self.cmd_Connect_Help)
        self.gcode.register_command(
            "PALETTE_DISCONNECT", self.cmd_Disconnect, desc=self.cmd_Disconnect_Help)
        self.serial = None
        self.serial_port = config.get("serial")
        if not self.serial_port:
            raise config.error("Invalid serial port specific for Palette 2")
        self.baud = config.getint("baud", default=250000)

        self.readThread = None
        self.writeThread = None
        self.writeQueue = Queue()
        self.heartbeat = None

    cmd_Connect_Help = ("Connect to the Palette 2")
    def cmd_Connect(self, gcmd):
        if self.serial:
            logging.warning("Palette 2 serial port is already active, disconnect first")
            return

        logging.info("Connecting to Palette 2 on port (%s) at (%s)" %(self.serial_port, self.baud))
        self.serial = serial.Serial(self.serial_port, self.baud, timeout=0.5)

        if self.readThread is None:
            self.readThread = threading.Thread(target=self.run_Read, args=(self.serial,))
            self.readThread.daemon = True
            self.readThread.start()
        if self.writeThread is None:
            self.writeThread = threading.Thread(target=self.run_Write, args=(self.serial,))
            self.writeThread.daemon = True
            self.writeThread.start()


    cmd_Disconnect_Help = ("Disconnect from the Palette 2")
    def cmd_Disconnect(self, gmcd=None):
        logging.info("Disconnecting from Palette 2")
        if self.serial:
            self.serial.close()
            self.serial = None

    def run_Read(self, serial):
        while serial.is_open:
            raw_line = serial.readline()
            if raw_line:
                # Line was return without timeout
                text_line = raw_line.decode().strip()
                logging.debug("%s <- P2 (%s)" %(time.time(), text_line))

                # Received a heartbeat from the device
                if text_line == COMMAND_HEARTBEAT:
                    self.heartbeat = time.time()

            # do a heartbeat check
            if self.heartbeat and self.heartbeat < (time.time() - HEARTBEAT_TIMEOUT):
                logging.error("P2 has not responded to heartbeat, Palette will disconnect")
                self.cmd_Disconnect()

    def run_Write(self, serial):
        # Tell the device we're alive
        lastHeartbeatSend = time.time()
        self.writeQueue.put("\n")
        self.writeQueue.put(COMMAND_HEARTBEAT)
        
        while serial.is_open:
            try: 
                text_line = self.writeQueue.get(True, 0.5)
                if text_line:
                    l = text_line.strip()
                    logging.debug("%s -> P2 (%s)" %(time.time(), l))
                    terminated_line = "%s\n" %(l)
                    serial.write(terminated_line.encode())
            except Empty:
                pass

            # Do heartbeat routine
            if lastHeartbeatSend < (time.time() - HEARTBEAT_SEND):
                self.writeQueue.put(COMMAND_HEARTBEAT)
                lastHeartbeatSend = time.time()

        with self.writeQueue.mutex:
            self.writeQueue.queue.clear()

def load_config(config):
    return Palette2(config)
