# Palette 2 MMU support, Firmware 9.0.9 and newer supported only!
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
COMMAND_CUT = "O10 D5"
COMMAND_CLEAR = [
        "O10 D5",
        "O10 D0 D0 D0 DFFE1",
        "O10 D1 D0 D0 DFFE1",
        "O10 D2 D0 D0 DFFE1",
        "O10 D3 D0 D0 DFFE1",
        "O10 D4 D0 D0 D0069"]
COMMAND_FILENAME = "O51"
COMMAND_FILENAMES_DONE = "O52" 
COMMAND_FIRMWARE = "O50"
COMMAND_PING = "O31"

HEARTBEAT_SEND = 5
HEARTBEAT_TIMEOUT = 11

INFO_NOT_CONNECTED = "Palette 2 is not connected, connect first"

class Palette2:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object("gcode")
        self.gcode.register_command(
            "PALETTE_CONNECT", self.cmd_Connect, desc=self.cmd_Connect_Help)
        self.gcode.register_command(
            "PALETTE_DISCONNECT", self.cmd_Disconnect, desc=self.cmd_Disconnect_Help)
        self.gcode.register_command(
            "PALETTE_CLEAR", self.cmd_Clear, desc=self.cmd_Clear_Help)
        self.gcode.register_command(
            "PALETTE_CUT", self.cmd_Cut, desc=self.cmd_Cut_Help)
        self.serial = None
        self.serial_port = config.get("serial")
        if not self.serial_port:
            raise config.error("Invalid serial port specific for Palette 2")
        self.baud = config.getint("baud", default=250000)
        self.feedrate_splice = config.getfloat("feedrate_splice", 0.8, minval=0., maxval=1.)
        self.feedrate_normal = config.getfloat("feedrate_normal", 1.0, minval=0., maxval=1.)

        # Omega code matchers
        self.omega_header = [None] * 9
        omega_handlers = ["O" + str(i) for i in range(33)]
        for cmd in omega_handlers:
            func = getattr(self, 'cmd_' + cmd, None)
            desc = getattr(self, 'cmd_' + cmd + '_help', None)
            if func:
                self.gcode.register_command(cmd, func, desc=desc)
            else:
                self.gcode.register_command(cmd, self.cmd_OmegaDefault)

        self._reset()

        self.read_thread = None
        self.write_thread = None
        self.write_queue = Queue()
        self.heartbeat = None

        self.is_printing = False

    def _reset(self):
        self.files = []
        self.omega_algorithms = []
        self.omega_algorithms_counter = 0
        self.omega_splices = []
        self.omega_splices_counter = 0
        self.omega_pings = []
        self.omega_pongs = []
        self.omega_current_ping = None
        self.omega_header = [None] * 9
        self.omega_header_counter = 0
        self.omega_last_command = ""
        self.omega_drivers = []

    def _check_P2(self, gcmd=None):
        if self.serial:
            return True
        if gcmd:
            gcmd.respond_info(INFO_NOT_CONNECTED)
        return False

    cmd_Connect_Help = ("Connect to the Palette 2")
    def cmd_Connect(self, gcmd):
        try:
            virtual_sdcard = self.printer.lookup_object("virtual_sdcard")
        except:
            raise self.printer.command_error("Palette 2 requires [virtual_sdcard] to work, please add it to your config!")

        try:
            pause_check = self.printer.lookup_object("pause_resume")
        except:
            raise self.printer.command_error("Palette 2 requires [pause_resume] to work, please add it to your config!")

        if self.serial:
            gcmd.respond_info("Palette 2 serial port is already active, disconnect first")
            return

        logging.info("Connecting to Palette 2 on port (%s) at (%s)" %(self.serial_port, self.baud))
        self.serial = serial.Serial(self.serial_port, self.baud, timeout=0.5)

        if self.read_thread is None:
            self.read_thread = threading.Thread(target=self._run_Read, args=(self.serial,))
            self.read_thread.daemon = True
        if self.write_thread is None:
            self.write_thread = threading.Thread(target=self._run_Write, args=(self.serial,))
            self.write_thread.daemon = True

        self.read_thread.start()
        self.write_thread.start()

    cmd_Disconnect_Help = ("Disconnect from the Palette 2")
    def cmd_Disconnect(self, gmcd=None):
        logging.info("Disconnecting from Palette 2")
        if self.serial:
            self.serial.close()
            self.serial = None

    cmd_Clear_Help = ("Clear the input and output of the Palette 2")
    def cmd_Clear(self, gcmd):
        logging.info("Clearing Palette 2 input and output")
        if self._check_P2(gcmd):
            for l in COMMAND_CLEAR:
                self.write_queue.put(l)

    cmd_Cut_Help = ("Cut the outgoing filament")
    def cmd_Cut(self, gcmd):
        logging.info("Cutting outgoing filament in Palette 2")
        if self._check_P2(gcmd):
            self.write_queue.put(COMMAND_CUT)

    def cmd_OmegaDefault(self, gcmd):
        logging.debug("Omega Code: %s" %(gcmd.get_command()))
        if self._check_P2(gcmd):
            self.write_queue.put(gcmd.get_commandline())

    cmd_O1_help = ("Initialize the print, and check connection with the Palette 2")
    def cmd_O1(self, gcmd):
        logging.info("Initializing print with Pallete 2")
        if self._check_P2(gcmd):
            startTs = time.time()
            while self.heartbeat is None and self.heartbeat < (time.time() - HEARTBEAT_TIMEOUT) and startTs > (time.time() - HEARTBEAT_TIMEOUT):
                time.sleep(1)

            if self.heartbeat < (time.time() - HEARTBEAT_TIMEOUT):
                raise self.printer.command_error("No response from Palette 2 when initializing")

            self.write_queue.put(gcmd.get_commandline())
            self.gcode.run_script("PAUSE")
            self.gcode.respond_info("Palette 2 waiting on user to complete setup")

    cmd_O9_help = ("Reset print information")
    def cmd_O9(self, gcmd):
        logging.info("Print finished, resetting Palette 2 state")
        if self._check_P2(gcmd):
            self.write_queue.put(gcmd.get_commandline())
        self.is_printing = False

    def cmd_O21(self, gcmd):
        logging.debug("Omega version: %s" %(gcmd.get_commandline()))
        self._reset()
        self.omega_header[0] = gcmd.get_commandline()
        self.is_printing = True

    def cmd_O22(self, gcmd):
        logging.debug("Omega printer profile: %s" %(gcmd.get_commandline()))
        self.omega_header[1] = gcmd.get_commandline()

    def cmd_O23(self, gcmd):
        logging.debug("Omega slicer profile: %s" %(gcmd.get_commandline()))
        self.omega_header[2] = gcmd.get_commandline()

    def cmd_O24(self, gcmd):
        logging.debug("Omega PPM: %s" %(gcmd.get_commandline()))
        self.omega_header[3] = gcmd.get_commandline()

    def cmd_O25(self, gcmd):
        logging.debug("Omega inputs: %s" %(gcmd.get_commandline()))
        self.omega_header[4] = gcmd.get_commandline()
        drives = self.omega_header[4][4:].split()
        for idx in range(len(drives)):
            state = drives[idx][:2]
            if state == "D1":
                drives[idx] = "U" + str(60 + idx)
        self.omega_drives = [d for d in drives if d != "D0"]
        logging.info("Omega drives: %s" %self.omega_drives)

    def cmd_O26(self, gcmd):
        logging.debug("Omega splices %s" %(gcmd.get_commandline()))
        self.omega_header[5] = gcmd.get_commandline()

    def cmd_O27(self, gcmd):
        logging.debug("Omega pings: %s" %(gcmd.get_commandline()))
        self.omega_header[6] = gcmd.get_commandline()

    def cmd_O28(self, gcmd):
        logging.debug("Omega MSF NA: %s" %(gcmd.get_commandline()))
        self.omega_header[7] = gcmd.get_commandline()

    def cmd_O29(self, gcmd):
        logging.debug("Omega MSF NH: %s" %(gcmd.get_commandline()))
        self.omega_header[8] = gcmd.get_commandline()

    def cmd_O30(self, gcmd):
        try:
            param_drive = gcmd.get_commandline()[5:6]
            param_distance = gcmd.get_commandline()[8:]
            self.omega_splices.append((int(param_drive), param_distance))
            logging.debug("Omega splice command drive %s distance %s" %(param_drive, param_distance))
        except:
            gcmd.respond_info("Incorrectly formatted splice command")

    def cmd_O31(self, gcmd):
        if self._check_P2(gcmd):
            try:
                self.omega_current_ping = gcmd.get_commandline()
                logging.debug("Omega ping command: %s" %(gcmd.get_commandline()))
            except:
                gcmd.respond_info("Incorrectly formatted ping command")

            self.write_queue.put(COMMAND_PING)
            self.gcode.create_gcode_command("G4", "G4", {"P": "10"})

    def cmd_O32(self, gcmd):
        logging.debug("Omega algorithm: %s" %(gcmd.get_commandline()))
        self.omega_algorithms.append(gcmd.get_commandline()[4:])

    def cmd_P2_O20(self, params):
        if not self.is_printing:
            return

        # First print, we can ignore
        if params[0] == "D5":
            logging.info("First print on Palette")
            return

        try:
            n = int(params[0][1:])
        except:
            logging.error("O20 command has invalid parameters")
            return

        if n == 0:
            logging.info("Sending omega header %s" %self.omega_header_counter)
            self.write_queue.put(self.omega_header[self.omega_header_counter])
            self.omega_header_counter = self.omega_header_counter + 1
        elif n == 1:
            logging.info("Sending splice info %s" %self.omega_splices_counter)
            splice = self.omega_splices[self.omega_splices_counter]
            self.write_queue.put("O30 D%d D%s" %(splice[0], splice[1]))
            self.omega_splices_counter = self.omega_splices_counter + 1
        elif n == 2:
            logging.info("Sending currnet ping info %s")
            self.write_queue.put(self.omega_current_ping)
        elif n == 4:
            logging.info("Sending algorithm info %s" %self.omega_algorithms_counter)
            self.write_queue.put(self.omega_algorithms[self.omega_algorithms_counter])
            self.omega_algorithms_counter = self.omega_algorithms_counter + 1
        elif n == 8:
            logging.info("Resending the last command to Palette 2")
            self.write_queue.put(self.omega_last_command)

    def cmd_P2_O34(self, params):
        if not self.is_printing:
            return

        if len(params) > 2:
            percent = float(params[1][1:])
            if params[0] == "D1":
                number = len(self.omega_pings) + 1
                d = { "number": number, "percent": percent }
                logging.info("Ping %d, %d percent" %(number, percent))
                self.omega_pings.append(d)
            elif params[0] == "D2":
                number = len(self.omega_pongs) + 1
                d = { "number": number, "percent": percent }
                logging.info("Pong %d, %d percent" %(number, percent))
                self.omega_pongs.append(d)

    def cmd_P2_O40(self, params):
        logging.info("Resume request from Palette 2")
        self.gcode.run_script("RESUME")

    def cmd_P2_O50(self, params):
        if len(params) > 1:
            try:
                fw = params[0][1:]
                logging.info("Palette 2 firmware version %s detected" %os.fwalk)
            except:
                logging.error("Unable to parse firmware version")

            if fw < "9.0.9":
                raise self.printer.command_error("Palette 2 firmware version is too old, update to at least 9.0.9")
        else:
            try:
                virtual_sdcard = self.printer.lookup_object("virtual_sdcard")
            except:
                pass

            if virtual_sdcard:
                self.files = [file for (file, size) in virtual_sdcard.get_file_list(check_subdirs=True) if ".mcf.gcode" in file]
                for file in self.files:
                    self.write_queue.put("%s D%s" %(COMMAND_FILENAME, file))
                self.write_queue.put(COMMAND_FILENAMES_DONE)

    def cmd_P2_O53(self, params):
        if len(params) > 1 and params[0] == "D1":
            try:
                idx = int(params[1][1:], 16)
                file = self.files[::-1][idx]
                self.gcode.run_script("SDCARD_PRINT_FILE FILENAME=%s" %file)
            except:
                logging.error("O53 has invalid command parameters")


    def cmd_P2_O88(self, params):
        logging.error("Palette 2 error detected")
        try:
            error = int(params[0][1:], 16)
            logging.error("Palette 2 error code %d" %error)
        except:
            logging.error("Unable to parse Palette 2 error")

    def cmd_P2_O97(self, params):
        def printCancelling(params):
            logging.debug("Print Cancelling")
            self.gcode.run_script("CANCEL_PRINT")

        def printCancelled(params):
            logging.debug("Print Cancelled")
            self.is_printing = False

        def loadingOffsetStart(params):
            logging.info("Waiting for user to load filament into printer")

        def loadingOffset(params):
            remaining = int(params[1][1:], 16)
            logging.debug("Loading filamant remaining %d" %remaining)
            # We could check offset and auto-start by sending "039 D1" to Palette 2

        def feedrateStart(params):
            logging.info("Setting feedrate to %f for splice" %self.feedrate_splice)
            self.gcode.run_script("M220 S%d" %(self.feedrate_splice * 100))

        def feedrateEnd(params):
            logging.info("Setting feedrate to %f splice done" %self.feedrate_normal)
            self.gcode.run_script("M220 S%d" %(self.feedrate_normal * 100))

        matchers = [ ]
        if self.is_printing:
            matchers = matchers + [
                [printCancelling, 2, "U0", "D2"],
                [printCancelled, 2, "U0", "D3"],
                [loadingOffsetStart, 1, "U39"],
                [loadingOffset, 2, "U39"]
            ]

        matchers.append([feedrateStart, 2, "U25", "D0"])
        matchers.append([feedrateEnd, 2, "U25", "D1"])
        self._param_Matcher(matchers, params)

    def cmd_P2_O100(self, params):
        logging.info("Pause request from Palette 2")
        self.gcode.run_script("RESUME")

    def cmd_P2_0102(self, params):
        logging.warning("Smart load isn't supported, ignoring command to start smart load")

    def cmd_P2(self, line):
        t = line.split()
        ocode = t[0]
        params = t[1:]
        params_count = len(params)
        if params_count:
            res = [i for i in params if i[0] == "D" or i[0] == "U"]
            if not all(res):
                logging.error("Omega parameters are invalid")
                return

        func = getattr(self, 'cmd_P2_' + ocode, None)
        if func is not None:
            func(params)

    def _param_Matcher(self, matchers, params):
        # Match the command with the handling table
        for matcher in matchers:
            if len(params) > matcher[1]:
                match_params = matcher[2:]
                res = all([match_params[i] == params[i] for i in range(len(match_params))])
                if res:
                    matcher[0](params)
                    return True

        return False

    def _run_Read(self, serial):
        while serial.is_open:
            raw_line = serial.readline()
            if raw_line:
                # Line was return without timeout
                text_line = raw_line.decode().strip()
                logging.debug("%s P2 -> : %s" %(time.time(), text_line))

                # Received a heartbeat from the device
                if text_line == COMMAND_HEARTBEAT:
                    self.heartbeat = time.time()

                elif text_line[0] == "O":
                    self.cmd_P2(text_line)

            # do a heartbeat check
            if self.heartbeat and self.heartbeat < (time.time() - HEARTBEAT_TIMEOUT):
                logging.error("P2 has not responded to heartbeat, Palette will disconnect")
                self.cmd_Disconnect()

    def _run_Write(self, serial):
        # Tell the device we're alive
        lastHeartbeatSend = time.time()
        self.write_queue.put("\n")
        self.write_queue.put(COMMAND_HEARTBEAT)
        self.write_queue.put(COMMAND_FIRMWARE)
        
        while serial.is_open:
            try: 
                text_line = self.write_queue.get(True, 0.5)
                if text_line:
                    self.omega_last_command = text_line
                    l = text_line.strip()
                    logging.debug("%s -> P2 : %s" %(time.time(), l))
                    terminated_line = "%s\n" %(l)
                    serial.write(terminated_line.encode())
            except Empty:
                pass

            # Do heartbeat routine
            if lastHeartbeatSend < (time.time() - HEARTBEAT_SEND):
                self.write_queue.put(COMMAND_HEARTBEAT)
                lastHeartbeatSend = time.time()

        with self.write_queue.mutex:
            self.write_queue.queue.clear()
        self.heartbeat = None
        self.is_printing = False

def load_config(config):
    return Palette2(config)
