# Copyright (c) 2015 Ultimaker B.V.
# Cura is released under the terms of the AGPLv3 or higher.

from UM.Signal import Signal, SignalEmitter
from . import USBPrinterOutputDevice
from UM.Application import Application
from UM.Resources import Resources
from UM.Logger import Logger
from UM.PluginRegistry import PluginRegistry
from UM.OutputDevice.OutputDevicePlugin import OutputDevicePlugin
from cura.PrinterOutputDevice import ConnectionState
from UM.Qt.ListModel import ListModel
from UM.Message import Message

from cura.CuraApplication import CuraApplication

import threading
import platform
import glob
import time
import os.path
from UM.Extension import Extension

from PyQt5.QtQml import QQmlComponent, QQmlContext
from PyQt5.QtCore import QUrl, QObject, pyqtSlot, pyqtProperty, pyqtSignal, Qt
from UM.i18n import i18nCatalog
i18n_catalog = i18nCatalog("cura")


##  Manager class that ensures that a usbPrinteroutput device is created for every connected USB printer.
class USBPrinterOutputDeviceManager(QObject, SignalEmitter, OutputDevicePlugin, Extension):
    def __init__(self, parent = None):
        super().__init__(parent = parent)
        self._serial_port_list = []
        self._usb_output_devices = {}
        self._usb_output_devices_model = None
        self._update_thread = threading.Thread(target = self._updateThread)
        self._update_thread.setDaemon(True)

        self._check_updates = True
        self._firmware_view = None

        ## Add menu item to top menu of the application.
        self.setMenuName(i18n_catalog.i18nc("@title:menu","Firmware"))
        self.addMenuItem(i18n_catalog.i18nc("@item:inmenu", "Update Firmware"), self.updateAllFirmware)

        Application.getInstance().applicationShuttingDown.connect(self.stop)
        self.addUSBOutputDeviceSignal.connect(self.addOutputDevice) #Because the model needs to be created in the same thread as the QMLEngine, we use a signal.

    addUSBOutputDeviceSignal = Signal()
    connectionStateChanged = pyqtSignal()

    progressChanged = pyqtSignal()

    @pyqtProperty(float, notify = progressChanged)
    def progress(self):
        progress = 0
        for printer_name, device in self._usb_output_devices.items(): # TODO: @UnusedVariable "printer_name"
            progress += device.progress

        return progress / len(self._usb_output_devices)

    def start(self):
        self._check_updates = True
        self._update_thread.start()

    def stop(self):
        self._check_updates = False
        try:
            self._update_thread.join()
        except RuntimeError:
            pass

    def _updateThread(self):
        while self._check_updates:
            result = self.getSerialPortList(only_list_usb = True)
            self._addRemovePorts(result)
            time.sleep(5)

    ##  Show firmware interface.
    #   This will create the view if its not already created.
    def spawnFirmwareInterface(self, serial_port):
        if self._firmware_view is None:
            path = QUrl.fromLocalFile(os.path.join(PluginRegistry.getInstance().getPluginPath("USBPrinting"), "FirmwareUpdateWindow.qml"))
            component = QQmlComponent(Application.getInstance()._engine, path)

            self._firmware_context = QQmlContext(Application.getInstance()._engine.rootContext())
            self._firmware_context.setContextProperty("manager", self)
            self._firmware_view = component.create(self._firmware_context)

        self._firmware_view.show()

    @pyqtSlot()
    def updateAllFirmware(self):
        if not self._usb_output_devices:
            Message(i18n_catalog.i18nc("@info","Cannot update firmware, there were no connected printers found.")).show()
            return

        self.spawnFirmwareInterface("")
        for printer_connection in self._usb_output_devices:
            try:
                self._usb_output_devices[printer_connection].updateFirmware(Resources.getPath(CuraApplication.ResourceTypes.Firmware, self._getDefaultFirmwareName()))
            except FileNotFoundError:
                self._usb_output_devices[printer_connection].setProgress(100, 100)
                Logger.log("w", "No firmware found for printer %s", printer_connection)
                continue

    @pyqtSlot(str, result = bool)
    def updateFirmwareBySerial(self, serial_port):
        if serial_port in self._usb_output_devices:
            self.spawnFirmwareInterface(self._usb_output_devices[serial_port].getSerialPort())
            try:
                self._usb_output_devices[serial_port].updateFirmware(Resources.getPath(CuraApplication.ResourceTypes.Firmware, self._getDefaultFirmwareName()))
            except FileNotFoundError:
                self._firmware_view.close()
                Logger.log("e", "Could not find firmware required for this machine")
                return False
            return True
        return False

    ##  Return the singleton instance of the USBPrinterManager
    @classmethod
    def getInstance(cls, engine = None, script_engine = None):
        # Note: Explicit use of class name to prevent issues with inheritance.
        if USBPrinterOutputDeviceManager._instance is None:
            USBPrinterOutputDeviceManager._instance = cls()

        return USBPrinterOutputDeviceManager._instance

    def _getDefaultFirmwareName(self):
        machine_instance = Application.getInstance().getMachineManager().getActiveMachineInstance()
        machine_type = machine_instance.getMachineDefinition().getId()
        if platform.system() == "Linux":
            baudrate = 115200
        else:
            baudrate = 250000

        # NOTE: The keyword used here is the id of the machine. You can find the id of your machine in the *.json file, eg.
        # https://github.com/Ultimaker/Cura/blob/master/resources/machines/ultimaker_original.json#L2
        # The *.hex files are stored at a seperate repository:
        # https://github.com/Ultimaker/cura-binary-data/tree/master/cura/resources/firmware
        machine_without_extras  = {"bq_witbox"                : "MarlinWitbox.hex",
                                   "bq_hephestos_2"           : "MarlinHephestos2.hex",
                                   "ultimaker_original"       : "MarlinUltimaker-{baudrate}.hex",
                                   "ultimaker_original_plus"  : "MarlinUltimaker-UMOP-{baudrate}.hex",
                                   "ultimaker2"               : "MarlinUltimaker2.hex",
                                   "ultimaker2_go"            : "MarlinUltimaker2go.hex",
                                   "ultimaker2plus"           : "MarlinUltimaker2plus.hex",
                                   "ultimaker2_extended"      : "MarlinUltimaker2extended.hex",
                                   "ultimaker2_extended_plus" : "MarlinUltimaker2extended-plus.hex",
                                   }
        machine_with_heated_bed = {"ultimaker_original"       : "MarlinUltimaker-HBK-{baudrate}.hex",
                                   }

        ##TODO: Add check for multiple extruders
        hex_file = None
        if machine_type in machine_without_extras.keys():  # The machine needs to be defined here!
            if machine_type in machine_with_heated_bed.keys() and machine_instance.getMachineSettingValue("machine_heated_bed"):
                Logger.log("d", "Choosing firmware with heated bed enabled for machine %s.", machine_type)
                hex_file = machine_with_heated_bed[machine_type]  # Return firmware with heated bed enabled
            else:
                Logger.log("d", "Choosing basic firmware for machine %s.", machine_type)
                hex_file = machine_without_extras[machine_type]  # Return "basic" firmware
        else:
            Logger.log("e", "There is no firmware for machine %s.", machine_type)

        if hex_file:
            return hex_file.format(baudrate=baudrate)
        else:
            Logger.log("e", "Could not find any firmware for machine %s.", machine_type)
            raise FileNotFoundError()

    ##  Helper to identify serial ports (and scan for them)
    def _addRemovePorts(self, serial_ports):
        # First, find and add all new or changed keys
        for serial_port in list(serial_ports):
            if serial_port not in self._serial_port_list:
                self.addUSBOutputDeviceSignal.emit(serial_port)  # Hack to ensure its created in main thread
                continue
        self._serial_port_list = list(serial_ports)

        devices_to_remove = []
        for port, device in self._usb_output_devices.items():
            if port not in self._serial_port_list:
                device.close()
                devices_to_remove.append(port)

        for port in devices_to_remove:
            del self._usb_output_devices[port]

    ##  Because the model needs to be created in the same thread as the QMLEngine, we use a signal.
    def addOutputDevice(self, serial_port):
        device = USBPrinterOutputDevice.USBPrinterOutputDevice(serial_port)
        device.connectionStateChanged.connect(self._onConnectionStateChanged)
        device.connect()
        device.progressChanged.connect(self.progressChanged)
        self._usb_output_devices[serial_port] = device

    ##  If one of the states of the connected devices change, we might need to add / remove them from the global list.
    def _onConnectionStateChanged(self, serial_port):
        try:
            if self._usb_output_devices[serial_port].connectionState == ConnectionState.connected:
                self.getOutputDeviceManager().addOutputDevice(self._usb_output_devices[serial_port])
            else:
                self.getOutputDeviceManager().removeOutputDevice(serial_port)
            self.connectionStateChanged.emit()
        except KeyError:
            pass  # no output device by this device_id found in connection list.


    @pyqtProperty(QObject , notify = connectionStateChanged)
    def connectedPrinterList(self):
        self._usb_output_devices_model = ListModel()
        self._usb_output_devices_model.addRoleName(Qt.UserRole + 1, "name")
        self._usb_output_devices_model.addRoleName(Qt.UserRole + 2, "printer")
        for connection in self._usb_output_devices:
            if self._usb_output_devices[connection].connectionState == ConnectionState.connected:
                self._usb_output_devices_model.appendItem({"name": connection, "printer": self._usb_output_devices[connection]})
        return self._usb_output_devices_model

    ##  Create a list of serial ports on the system.
    #   \param only_list_usb If true, only usb ports are listed
    def getSerialPortList(self, only_list_usb = False):
        base_list = []
        if platform.system() == "Windows":
            import winreg
            try:
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,"HARDWARE\\DEVICEMAP\\SERIALCOMM")
                i = 0
                while True:
                    values = winreg.EnumValue(key, i)
                    if not only_list_usb or "USBSER" in values[0]:
                        base_list += [values[1]]
                    i += 1
            except Exception as e:
                pass
        else:
            if only_list_usb:
                base_list = base_list + glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*") + glob.glob("/dev/cu.usb*")
                base_list = filter(lambda s: "Bluetooth" not in s, base_list) # Filter because mac sometimes puts them in the list
            else:
                base_list = base_list + glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*") + glob.glob("/dev/cu.*") + glob.glob("/dev/tty.usb*") + glob.glob("/dev/rfcomm*") + glob.glob("/dev/serial/by-id/*")
        return list(base_list)

    _instance = None
