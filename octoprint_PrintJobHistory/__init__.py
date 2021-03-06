# coding=utf-8
from __future__ import absolute_import

import threading
import time

import octoprint.plugin
from octoprint.events import Events

import datetime
import math
import flask
import os
import shutil
import tempfile

from octoprint_PrintJobHistory.common import CSVExportImporter
from octoprint_PrintJobHistory.models.FilamentModel import FilamentModel
from octoprint_PrintJobHistory.models.PrintJobModel import PrintJobModel
from octoprint_PrintJobHistory.models.TemperatureModel import TemperatureModel
from peewee import DoesNotExist

from .common.SettingsKeys import SettingsKeys
from .common.SlicerSettingsParser import SlicerSettingsParser
from .api.PrintJobHistoryAPI import PrintJobHistoryAPI
from .api import TransformPrintJob2JSON
from .DatabaseManager import DatabaseManager
from .CameraManager import CameraManager


class PrintJobHistoryPlugin(
							PrintJobHistoryAPI,
							octoprint.plugin.SettingsPlugin,
                            octoprint.plugin.AssetPlugin,
                            octoprint.plugin.TemplatePlugin,
							octoprint.plugin.StartupPlugin,
							octoprint.plugin.EventHandlerPlugin,
							octoprint.plugin.SimpleApiPlugin
							):


	def initialize(self):
		self._preHeatPluginImplementation = None
		self._preHeatPluginImplementationState = None
		self._filamentManagerPluginImplementation = None
		self._filamentManagerPluginImplementationState = None
		self._displayLayerProgressPluginImplementation = None
		self._displayLayerProgressPluginImplementationState = None

		pluginDataBaseFolder = self.get_plugin_data_folder()

		self._logger.info("Start initializing")
		# DATABASE
		sqlLoggingEnabled = self._settings.get_boolean([SettingsKeys.SETTINGS_KEY_SQL_LOGGING_ENABLED])
		self._databaseManager = DatabaseManager(self._logger, sqlLoggingEnabled)
		self._databaseManager.initDatabase(pluginDataBaseFolder, self._sendErrorMessageToClient)

		# CAMERA
		self._cameraManager = CameraManager(self._logger)
		pluginBaseFolder = self._basefolder

		self._cameraManager.initCamera(pluginDataBaseFolder, pluginBaseFolder, self._settings)

		# Init values for initial settings view-page
		self._settings.set( [SettingsKeys.SETTINGS_KEY_DATABASE_PATH], self._databaseManager.getDatabaseFileLocation())
		self._settings.set( [SettingsKeys.SETTINGS_KEY_SNAPSHOT_PATH], self._cameraManager.getSnapshotFileLocation())
		self._settings.save()

		# OTHER STUFF
		self._currentPrintJobModel = None

		self.alreadyCanceled = False

		self._logger.info("Done initializing")

	################################################################################################## private functions
	def _sendDataToClient(self, payloadDict):
		self._plugin_manager.send_plugin_message(self._identifier,
												 payloadDict)


	def _sendErrorMessageToClient(self, title, message):
		self._sendDataToClient(dict(action="errorPopUp",
									title= title,
									message=message))

	def _checkForMissingPluginInfos(self, sendToClient=False):

		pluginInfo = self._getPluginInformation("preheat")
		self._preHeatPluginImplementationState = pluginInfo[0]
		self._preHeatPluginImplementation = pluginInfo[1]

		pluginInfo = self._getPluginInformation("filamentmanager")
		self._filamentManagerPluginImplementationState  = pluginInfo[0]
		self._filamentManagerPluginImplementation = pluginInfo[1]

		pluginInfo = self._getPluginInformation("DisplayLayerProgress")
		self._displayLayerProgressPluginImplementationState  = pluginInfo[0]
		self._displayLayerProgressPluginImplementation = pluginInfo[1]

		self._logger.info("Plugin-State: "
						  "PreHeat=" + self._preHeatPluginImplementationState + " "
						  "DisplayLayerProgress=" + self._displayLayerProgressPluginImplementationState + " "
						  "filamentmanager=" + self._filamentManagerPluginImplementationState)

		if sendToClient == True:
			missingMessage = ""

			if self._preHeatPluginImplementation == None:
				missingMessage = missingMessage + "<li>PreHeat (<b>" + self._preHeatPluginImplementationState + "</b>)</li>"

			if self._filamentManagerPluginImplementation == None:
				missingMessage = missingMessage + "<li>FilamentManager (<b>" + self._filamentManagerPluginImplementationState + "</b>)</li>"

			if self._displayLayerProgressPluginImplementation == None:
				missingMessage = missingMessage + "<li>DisplayLayerProgress (<b>" + self._displayLayerProgressPluginImplementationState + "</b>)</li>"

			if missingMessage != "":
				missingMessage = "<ul>" + missingMessage + "</ul>"
				self._sendDataToClient(dict(action="missingPlugin",
											message=missingMessage))


	# get the plugin with status information
	# [0] == status-string
	# [1] == implementaiton of the plugin
	def _getPluginInformation(self, pluginKey):

		status = None
		implementation = None

		if pluginKey in self._plugin_manager.plugins:
			plugin = self._plugin_manager.plugins[pluginKey]
			if plugin != None:
				if (plugin.enabled == True):
					status = "enabled"
					# for OP 1.4.x we need to check agains "icompatible"-attribute
					if (hasattr(plugin, 'incompatible') ):
						if (plugin.incompatible == False):
							implementation = plugin.implementation
						else:
							status = "incompatible"
					else:
						# OP 1.3.x
						implementation = plugin.implementation
					pass
				else:
					status = "disabled"
		else:
			status = "missing"

		return [status, implementation]


	# Grabs all informations for the filament attributes
	def _createAndAssignFilamentModel(self, printJob, payload):
		filemanentModel  = FilamentModel()

		fileData = self._file_manager.get_metadata(payload["origin"], payload["path"])

		filamentLength = None
		if "analysis" in fileData:
			if "filament" in fileData["analysis"]:
				if "tool0" in fileData["analysis"]["filament"]:
					filamentLength = fileData["analysis"]["filament"]["tool0"]['length']

		filemanentModel.calculatedLength = filamentLength

		if self._filamentManagerPluginImplementation != None:

			filemanentModel.usedLength = self._filamentManagerPluginImplementation.filamentOdometer.totalExtrusion[0]
			selectedSpool = self._filamentManagerPluginImplementation.filamentManager.get_all_selections(self._filamentManagerPluginImplementation.client_id)
			if  selectedSpool != None and len(selectedSpool) > 0:
				spoolData = selectedSpool[0]["spool"]
				spoolName = spoolData["name"]
				spoolCost = spoolData["cost"]
				spoolCostUnit = self._filamentManagerPluginImplementation._settings.get(["currencySymbol"])
				spoolWeight = spoolData["weight"]

				profileData = selectedSpool[0]["spool"]["profile"]
				diameter = profileData["diameter"]
				material = profileData["material"]
				vendor = profileData["vendor"]
				density = profileData["density"]

				filemanentModel.spoolName = spoolName
				filemanentModel.spoolCost = spoolCost
				filemanentModel.spoolCostUnit = spoolCostUnit
				filemanentModel.spoolWeight = spoolWeight

				filemanentModel.profileVendor = vendor
				filemanentModel.diameter = diameter
				filemanentModel.density = density
				filemanentModel.material = material

				radius = diameter / 2;
				volume = filemanentModel.usedLength * math.pi * radius * radius / 1000;
				usedWeight = volume * density

				filemanentModel.usedWeight = usedWeight
				filemanentModel.usedCost = spoolCost / spoolWeight * usedWeight

		printJob.addFilamentModel(filemanentModel)
		pass

	def _updatePrintJobModelWithLayerHeightInfos(self, payload):
		totalLayers = payload["totalLayer"]
		currentLayer = payload["currentLayer"]
		self._currentPrintJobModel.printedLayers = currentLayer + " / " + totalLayers

		totalHeightWithExtrusion = payload["totalHeightWithExtrusion"]
		currentHeight = payload["currentHeight"]
		self._currentPrintJobModel.printedHeight = currentHeight + " / " + totalHeightWithExtrusion

	def _createPrintJobModel(self, payload):
		self._currentPrintJobModel = PrintJobModel()
		self._currentPrintJobModel.printStartDateTime = datetime.datetime.now()

		self._currentPrintJobModel.fileOrigin = payload["origin"]
		self._currentPrintJobModel.fileName = payload["name"]
		self._currentPrintJobModel.filePathName = payload["path"]

		# self._file_manager.path_on_disk()
		if "owner" in payload:
			self._currentPrintJobModel.userName = payload["owner"]
		else:
			self._currentPrintJobModel.userName = "John Doe"
		self._currentPrintJobModel.fileSize = payload["size"]

		tempFound = False
		tempTool = 0
		tempBed = 0

		if self._preHeatPluginImplementation != None:
			path_on_disk = octoprint.server.fileManager.path_on_disk(self._currentPrintJobModel.fileOrigin, self._currentPrintJobModel.filePathName)

			preHeatTemperature = self._preHeatPluginImplementation.read_temperatures_from_file(path_on_disk)
			if preHeatTemperature != None:
				if "bed" in preHeatTemperature:
					tempBed = preHeatTemperature["bed"]
					tempFound = True
				if "tool0" in preHeatTemperature:
					tempTool = preHeatTemperature["tool0"]
					tempFound = True
			pass
		else:
			# because temperature is 0 at the beginning, we need to wait a couple of seconds (maybe 3)
			self._readAndAssignCurrentTemperatureDelayed(self._currentPrintJobModel)

		if (tempFound == True):
			self._addTemperatureToPrintModel(self._currentPrintJobModel, tempBed, tempTool)



	def _readCurrentTemeratureAsync(self, printer, printJobModel, addTemperatureToPrintModel):
		time.sleep(10)
		currentTemps = printer.get_current_temperatures()
		if (currentTemps != None and "bed" in currentTemps and "tool0" in currentTemps):
			tempBed = currentTemps["bed"]["target"]
			tempTool = currentTemps["tool0"]["target"]
			addTemperatureToPrintModel(printJobModel, tempBed, tempTool)


	def _readAndAssignCurrentTemperatureDelayed(self, printJobModel):
		thread = threading.Thread(name='ReadCurrentTemperature',
								  target=self._readCurrentTemeratureAsync,
								  args=(self._printer, printJobModel, self._addTemperatureToPrintModel,))
		thread.daemon = True
		thread.start()
		pass


	def _addTemperatureToPrintModel(self, printJobModel, bedTemp, toolTemp):
		tempModel = TemperatureModel()
		tempModel.sensorName = "bed"
		tempModel.sensorValue = bedTemp
		printJobModel.addTemperatureModel(tempModel)

		tempModel = TemperatureModel()
		tempModel.sensorName = "tool0"
		tempModel.sensorValue = toolTemp
		printJobModel.addTemperatureModel(tempModel)

	#### print job finished
	def _printJobFinished(self, printStatus, payload):
		captureMode = self._settings.get([SettingsKeys.SETTINGS_KEY_CAPTURE_PRINTJOBHISTORY_MODE])
		if (captureMode == SettingsKeys.KEY_CAPTURE_PRINTJOBHISTORY_MODE_NONE):
			return

		captureThePrint = False
		if (captureMode == SettingsKeys.KEY_CAPTURE_PRINTJOBHISTORY_MODE_ALWAYS):
			captureThePrint = True
		else:
			# check status is neccessary
			if (printStatus == "success"):
				captureThePrint = True

		self._logger.info("Print result:" + printStatus + ", CaptureMode:"+captureMode)
		# capture the print
		if (captureThePrint == True):
			self._logger.info("Start capturing print job")
			# Core Data
			self._currentPrintJobModel.printEndDateTime = datetime.datetime.now()
			self._currentPrintJobModel.duration = (
						self._currentPrintJobModel.printEndDateTime - self._currentPrintJobModel.printStartDateTime).total_seconds()
			self._currentPrintJobModel.printStatusResult = printStatus

			# Slicer Settings
			selectedFilename = payload.get("path")
			selectedFile = self._file_manager.path_on_disk(payload.get("origin"), selectedFilename)
			slicerSettings = SlicerSettingsParser(self._logger).extractSlicerSettings(selectedFile, None)
			if (slicerSettings.settingsAsText != None and len(slicerSettings.settingsAsText) != 0):
				self._currentPrintJobModel.slicerSettingsAsText = slicerSettings.settingsAsText

			# Image
			if self._settings.get_boolean([SettingsKeys.SETTINGS_KEY_TAKE_SNAPSHOT_AFTER_PRINT]):
				self._cameraManager.takeSnapshotAsync(
														CameraManager.buildSnapshotFilename(self._currentPrintJobModel.printStartDateTime),
														self._sendErrorMessageToClient
													 )

			if self._settings.get_boolean([SettingsKeys.SETTINGS_KEY_TAKE_PLUGIN_THUMBNAIL_AFTER_PRINT]):
				metadata = self._file_manager.get_metadata(payload["origin"], payload["path"])
				# check if available
				if ("thumbnail" in metadata):
					self._cameraManager.takeThumbnailAsync(
						CameraManager.buildSnapshotFilename(self._currentPrintJobModel.printStartDateTime),
						metadata["thumbnail"])
				else:
					self._logger.warn("Thumbnail not found in print metadata")

			# FilamentInformations e.g. length
			self._createAndAssignFilamentModel(self._currentPrintJobModel, payload)

			# store everything in the database
			databaseId = self._databaseManager.insertPrintJob(self._currentPrintJobModel)

			printJobItem = None
			if self._settings.get_boolean([SettingsKeys.SETTINGS_KEY_SHOW_PRINTJOB_DIALOG_AFTER_PRINT]):

				self._settings.set_int([SettingsKeys.SETTINGS_KEY_SHOW_PRINTJOB_DIALOG_AFTER_PRINT_JOB_ID], databaseId)
				self._settings.save()

				# inform client to show job edit dialog
				printJobModel = self._databaseManager.loadPrintJob(databaseId)

				# check the correct status (redundent code, see event client_open)
				printJobItem = None
				showDisplayAfterPrintMode = self._settings.get(
					[SettingsKeys.SETTINGS_KEY_SHOWPRINTJOBDIALOGAFTERPRINT_MODE])
				printJobModelStatus = printJobModel.printStatusResult

				if (showDisplayAfterPrintMode == SettingsKeys.KEY_SHOWPRINTJOBDIALOGAFTERPRINT_MODE_SUCCESSFUL):
					# show only when succesfull
					if ("success" == printJobModelStatus):
						printJobItem = TransformPrintJob2JSON.transformPrintJobModel(printJobModel)
				else:
					# always
					printJobItem = TransformPrintJob2JSON.transformPrintJobModel(printJobModel)

			# inform client for a reload
			payload = {
				"action": "printFinished",
				"printJobItem": printJobItem	# if present then the editor dialog is shown
			}
			self._sendDataToClient(payload)
		else:
			self._logger.info("Snapshot not captured, because not activated")

	#######################################################################################   OP - HOOKs
	def on_after_startup(self):
		# check if needed plugins were available
		self._checkForMissingPluginInfos()

	def on_event(self, event, payload):
		# WebBrowser opened
		if Events.CLIENT_OPENED == event:
			# Send plugin storage information
			## Storage
			if (hasattr(self, "_databaseManager") == True):
				databaseFileLocation = self._databaseManager.getDatabaseFileLocation()
				snapshotFileLocation = self._cameraManager.getSnapshotFileLocation()

				self._sendDataToClient(dict(action="updateStorageInformation",
											databaseFileLocation = databaseFileLocation,
											snapshotFileLocation = snapshotFileLocation
											))
			# Check if all needed Plugins are available, if not modale dialog to User
			if self._settings.get_boolean([SettingsKeys.SETTINGS_KEY_PLUGIN_DEPENDENCY_CHECK]):
				self._checkForMissingPluginInfos(True)

			# Show last Print-Dialog
			if self._settings.get_boolean([SettingsKeys.SETTINGS_KEY_SHOW_PRINTJOB_DIALOG_AFTER_PRINT]):
				printJobId = self._settings.get_int([SettingsKeys.SETTINGS_KEY_SHOW_PRINTJOB_DIALOG_AFTER_PRINT_JOB_ID])
				if (not printJobId == None):
					try:
						printJobModel = self._databaseManager.loadPrintJob(printJobId)

						# check the correct status
						printJobItem = None
						showDisplayAfterPrintMode = self._settings.get([SettingsKeys.SETTINGS_KEY_SHOWPRINTJOBDIALOGAFTERPRINT_MODE])
						printJobModelStatus = printJobModel.printStatusResult

						if (showDisplayAfterPrintMode == SettingsKeys.KEY_SHOWPRINTJOBDIALOGAFTERPRINT_MODE_SUCCESSFUL):
							# show only when succesfull
							if ("success"== printJobModelStatus):
								printJobItem = TransformPrintJob2JSON.transformPrintJobModel(printJobModel)
						else:
							# always
							printJobItem = TransformPrintJob2JSON.transformPrintJobModel(printJobModel)

						payload = {
							"action": "showPrintJobDialogAfterClientConnection",
							"printJobItem": printJobItem
						}
						self._sendDataToClient(payload)
					except DoesNotExist as e:
						self._settings.remove([SettingsKeys.SETTINGS_KEY_SHOW_PRINTJOB_DIALOG_AFTER_PRINT_JOB_ID])

		elif Events.PRINT_STARTED == event:
			self.alreadyCanceled = False
			self._createPrintJobModel(payload)

		elif "DisplayLayerProgress_layerChanged" == event or event == "DisplayLayerProgress_heightChanged":
			self._updatePrintJobModelWithLayerHeightInfos(payload)

		elif Events.PRINT_DONE == event:
			self._printJobFinished("success", payload)
		elif Events.PRINT_FAILED == event:
			if self.alreadyCanceled == False:
				self._printJobFinished("failed", payload)
		elif Events.PRINT_CANCELLED == event:
			self.alreadyCanceled = True
			self._printJobFinished("canceled", payload)

		pass

	def on_settings_save(self, data):
		# default save function
		octoprint.plugin.SettingsPlugin.on_settings_save(self, data)

		sqlLoggingEnabled = self._settings.get_boolean([SettingsKeys.SETTINGS_KEY_SQL_LOGGING_ENABLED])
		self._databaseManager.showSQLLogging(sqlLoggingEnabled)

	# to allow the frontend to trigger an GET call
	def on_api_get(self, request):
		if len(request.values) != 0:
			action = request.values["action"]

			# deceide if you want the reset function in you settings dialog
			if "isResetSettingsEnabled" == action:
				return flask.jsonify(enabled="true")

			if "resetSettings" == action:
				self._settings.set([], self.get_settings_defaults())
				self._settings.save()
				return flask.jsonify(self.get_settings_defaults())
		pass

	##~~ SettingsPlugin mixin
	def get_settings_defaults(self):
		settings = dict()
		## Genral
		settings[SettingsKeys.SETTINGS_KEY_PLUGIN_DEPENDENCY_CHECK] = True
		settings[SettingsKeys.SETTINGS_KEY_SHOW_PRINTJOB_DIALOG_AFTER_PRINT] = True
		settings[SettingsKeys.SETTINGS_KEY_SHOW_PRINTJOB_DIALOG_AFTER_PRINT_JOB_ID] = None
		settings[SettingsKeys.SETTINGS_KEY_SHOWPRINTJOBDIALOGAFTERPRINT_MODE] = SettingsKeys.KEY_SHOWPRINTJOBDIALOGAFTERPRINT_MODE_SUCCESSFUL
		settings[SettingsKeys.SETTINGS_KEY_CAPTURE_PRINTJOBHISTORY_MODE] = SettingsKeys.KEY_CAPTURE_PRINTJOBHISTORY_MODE_SUCCESSFUL

		## Camera
		settings[SettingsKeys.SETTINGS_KEY_TAKE_SNAPSHOT_AFTER_PRINT] = True
		settings[SettingsKeys.SETTINGS_KEY_TAKE_PLUGIN_THUMBNAIL_AFTER_PRINT] = True

		## Export / Import
		settings[SettingsKeys.SETTINGS_KEY_IMPORT_CSV_MODE] = SettingsKeys.KEY_IMPORTCSV_MODE_APPEND

		## Debugging
		settings[SettingsKeys.SETTINGS_KEY_SQL_LOGGING_ENABLED] = False

		# ## Storage
		# if (hasattr(self,"_databaseManager") == True):
		# 	settings[SettingsKeys.SETTINGS_KEY_DATABASE_PATH] = self._databaseManager.getDatabaseFileLocation()
		# 	settings[SettingsKeys.SETTINGS_KEY_SNAPSHOT_PATH] = self._cameraManager.getSnapshotFileLocation()
		# else:
		# 	settings[SettingsKeys.SETTINGS_KEY_DATABASE_PATH] = ""
		# 	settings[SettingsKeys.SETTINGS_KEY_SNAPSHOT_PATH] = ""

		return settings

	##~~ TemplatePlugin mixin
	def get_template_configs(self):
		return [
			dict(type="tab", name="Print Job History"),
			dict(type="settings", custom_bindings=True)
		]

	def get_template_vars(self):
		return dict(
			apikey = self._settings.global_get(["api","key"])
		)

	##~~ AssetPlugin mixin
	def get_assets(self):
		# Define your plugin's asset files to automatically include in the
		# core UI here.
		return dict(
			js=["js/PrintJobHistory.js",
				"js/PrintJobHistory-APIClient.js",
				"js/PrintJobHistory-PluginCheckDialog.js",
				"js/PrintJobHistory-EditJobDialog.js",
				"js/PrintJobHistory-ImportDialog.js",
				"js/PrintJobHistory-ComponentFactory.js",
				"js/quill.min.js",
				"js/TableItemHelper.js",
				"js/ResetSettingsUtilV2.js"],
			css=["css/PrintJobHistory.css",
				 "css/quill.snow.css"],
			less=["less/PrintJobHistory.less"]
		)

	##~~ Softwareupdate hook
	def get_update_information(self):
		# Define the configuration for your plugin to use with the Software Update
		# Plugin here. See https://github.com/foosel/OctoPrint/wiki/Plugin:-Software-Update
		# for details.
		return dict(
			PrintJobHistory=dict(
				displayName="PrintJobHistory Plugin",
				displayVersion=self._plugin_version,

				# version check: github repository
				type="github_release",
				user="OllisGit",
				repo="OctoPrint-PrintJobHistory",
				current=self._plugin_version,

				# update method: pip
				pip="https://github.com/OllisGit/OctoPrint-PrintJobHistory/releases/latest/download/master.zip"
			)
		)

	# Increase upload-size (default 100kb) for uploading images
	def bodysize_hook(self, current_max_body_sizes, *args, **kwargs):
		return [("POST", r"/upload/", 5 * 1024 * 1024)]	# size in bytes


	# # For Streaming I need a special ResponseHandler
	# def route_hook(self, server_routes, *args, **kwargs):
	# 	from octoprint.server.util.tornado import LargeResponseHandler, UrlProxyHandler, path_validation_factory
	# 	from octoprint.util import is_hidden_path
	#
	# 	return [
	# 		# (r'myvideofeed', StreamHandler, dict(url=self._settings.global_get(["webcam", "snapshot"]),
	# 		# 									 as_attachment=True)),
	# 		(r"mysnapshot", UrlProxyHandler, dict(url=self._settings.global_get(["webcam", "snapshot"]),
	# 											 as_attachment=True))
	# 	]


# If you want your plugin to be registered within OctoPrint under a different name than what you defined in setup.py
# ("OctoPrint-PluginSkeleton"), you may define that here. Same goes for the other metadata derived from setup.py that
# can be overwritten via __plugin_xyz__ control properties. See the documentation for that.
# Name is used in the left Settings-Menue
__plugin_name__ = "PrintJobHistory"
__plugin_pythoncompat__ = ">=2.7,<4"

def __plugin_load__():
	global __plugin_implementation__
	__plugin_implementation__ = PrintJobHistoryPlugin()

	global __plugin_hooks__
	__plugin_hooks__ = {
		# "octoprint.server.http.routes": __plugin_implementation__.route_hook,
		"octoprint.server.http.bodysize": __plugin_implementation__.bodysize_hook,
		"octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information
	}



