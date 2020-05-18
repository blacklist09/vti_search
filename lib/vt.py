#!/usr/bin/env python3

import vt
import requests
import sys
import os.path
import json
import re
import asyncio

from .sandboxes import Sandbox_Parser

"""
    Keywords and description:
    https://developers.virustotal.com/v3.0/reference#files  

    - Hashes like md5, sha1 and sha256 that identifies it
    - size of the file
    - first_submission_date when the file was first received in VirusTotal (as a UNIX timestamp)
    - last_submission_date last time we received it (as a UNIX timestamp)
    - last_analysis_date last time we analysed it (as a UNIX timestamp)
    - last_modification_date last time the object itself was modified (as a UNIX timestamp)
    - times_submitted how many times VirusTotal had received it
    - last_analysis_results: result of the last analysis. 
        
        dict with AV name as key and a dict with notes/result from that scanner as value.
        category: normalized result. can be:
        
        - "harmless" (AV thinks the file is not malicious),
        - "undetected" (AV has no opinion about this file),
        - "suspicious" (AV thinks the file is suspicious),
        - "malicious" (AV thinks the file is malicious).

    - names we have seen the file with, being meaningful_name the one we consider more interesting
    - unique_sources indicates from how many different sources the file has been received


    In the attributes dictionary you are going to find also fields with information extracted from the file itself. We characterise the file and expose this information in the following keys:

    - type_description describe the type of file it is, being type_tag it short and you can use to search files of the same kind.
    - creation_date is extracted when possible from the file and indicates the timestamp the compilation or build tool give to it when created, it can be also faked by malware creators.
    - total_votes received from the VirusTotal community, each time a user vote a file it is reflected in this values. reputation field is calculated from the votes the file received and the users reputations credits.
    - vhash an in-house similarity clustering algorithm value, based on a simple structural feature hash allows you to find similar files
    - tags are extracted from different parts of the report and are labels that help you search similar samples

    Additionally VirusTotal together with each Antivirus scan runs a set of tool that allows us to collect more information about the file. All this tool information is included in the "attributes" key, together with the rest of fields previously described.

"""

# Translation map for internal objects
KEYWORD_MAP = {
        # file attributes
        "md5"                   :   "MD5",
        "sha1"                  :   "Sha1",
        "vhash"                 :   "VHash",
        "first_submission_date" :   "First submission",
        "last_submission_date"  :   "Last submission",
        "times_submitted"       :   "Number of submissions",
        "unique_sources"        :   "Unique sources",
        "size"                  :   "Size",
        "type_tag"              :   "Type",
        "tags"                  :   "Tag(s)",
        "magic"                 :   "File description",

        # domain attributes
        "creation_date"         :   "Creation date",
        "last_modification_date":   "Last modified",
        "last_update_date"      :   "Last updated",
        "registrar"             :   "Registrar",
        
        # url attributes
        "title"                 :   "Title",
        "last_final_url"        :   "Final URL",

        # attributes for scan results
        "harmless"              :   "Benign",
        "suspicious"            :   "Suspicious",
        "malicious"             :   "Malicious",
        "undetected"            :   "Undetected",
        "failure"               :   "Failure",
        "type-unsupported"      :   "Unsupported",
}


class VirusTotal_Search():
    """ Provides a class for running a VirusTotal Intelligence search and processing respective
        results.

        By default, at max 300 results are returned per query.
    """

    def __init__(self, options):

        self.options = options
        self.auxiliary = options["auxiliary"]

        self.site = {
                        
                        "url"       :   "https://virustotal.com/api/v3/",
                        "header"    :   {
                                            "x-apikey"  :   self.options["virustotal"]
                                        }
                    }

        self.client = vt.Client(self.options["virustotal"])
        
        self.sample_queue = asyncio.Queue()
        self.behavior_queue = asyncio.Queue()
        self.info_queue = asyncio.Queue()


    def display_scanning_results(self, sample, required_verbose_level = 0, file_handle = None):
        """ Displays scanning results per anti-virus vendor

            :param results: A dictionary of scan results saved in last_analysis_results
        """

        results = sample.last_analysis_results
        for item in results:
            engine = results[item]
            
            # category can be, e.g., suspicious, malicious, undetected, etc. 
            category = KEYWORD_MAP[engine["category"]] if engine["category"] in KEYWORD_MAP else engine["category"]         
            signature = engine["result"] if engine["result"] is not None else "--"
            if len(signature) > 40: signature = "{0} (...)".format(signature[:40])

            if "engine_update" in engine and engine["engine_update"] is not None:
                signature_database = engine["engine_update"] 
            else:
                signature_database = "--"

            string = "{0}{1:28}{2:47}{3:25}(Signature Database: {4})".format(" " * 2, engine["engine_name"], signature, category, signature_database)
            if self.options["verbose"] >= required_verbose_level: print(string)
            if file_handle is not None: file_handle.write("{0}\n".format(string))

            if self.options["csv"] and self.options["verbose"] >= 3:
                line = ""
                attributes = dir(sample)

                if sample.type == "file":
                    fields = ["sha256", "md5", "sha1", "vhash", "size", "type_tag", "tags"]
                elif sample.type == "domain":
                    fields = ["id", "registrar", "tags"]
                elif sample.type == "url":
                    fields = ["url", "last_final_url", "title", "tags"]
                else:
                    fields = []

                for value in fields:
                    if value not in attributes:
                        line += self.options["separator"]
                        continue

                    if isinstance(getattr(sample, value), list):
                        list_items = ""
                        for item in getattr(sample, value):
                            list_items += "{0}|".format(item)
                        line += "\"{0}\"{1}".format(list_items[:-1], self.options["separator"])
                    else:
                        line += "\"{0}\"{1}".format(getattr(sample, value), self.options["separator"])
                for value in ["engine_name", "result", "category", "engine_update"]:

                    if value in engine and engine[value] is not None:
                        line += "\"{0}\"{1}".format(engine[value], self.options["separator"]) 
                    else:
                        line += "\"\"{0}".format(self.options["separator"])
                
                self.options["csv_files"][sample.type].write("{0}\n".format(line[:-1]))
                
        if self.options["verbose"] >= required_verbose_level: print()
        if file_handle is not None: file_handle.write("\n")


    def display_values(self, id_list, sample, filter_values = None, required_verbose_level = 0, file_handle = None):
        """
            :param id_list:                 List of attributes that should be processed
            :param sample:                  The sample object
            :param filter_values:           White list of values that should be exclusively considered
                                            when parsing an attribute list
            :param required_verbose_level:  Displays results on screen if the verbose level
                                            is high enough, otherwise only logs results to a file
        """

        for value in id_list:
            if value not in dir(sample): continue
            
            if isinstance(getattr(sample, value), dict):
                for item in getattr(sample, value):
                    if filter_values is not None and isinstance(filter_values, list):
                        if item not in filter_values: continue

                    label = KEYWORD_MAP[item] if item in KEYWORD_MAP else item

                    string = "{0}{1:28}{2}".format(" " * 2, label + ":", getattr(sample, value)[item])
                    if self.options["verbose"] >= required_verbose_level: print(string)
                    if file_handle is not None: file_handle.write("{0}\n".format(string))
            elif isinstance(getattr(sample, value), list):
                line = ""
                for item in getattr(sample, value):
                    line += "{0}, ".format(item)
                label = KEYWORD_MAP[value] if value in KEYWORD_MAP else value

                string = "{0}{1:28}{2}".format(" " * 2, label + ":", line[:-2])
                if self.options["verbose"] >= required_verbose_level: print(string)
                if file_handle is not None: file_handle.write("{0}\n".format(string))
            else:
                label = KEYWORD_MAP[value] if value in KEYWORD_MAP else value
                string = "{0}{1:28}{2}".format(" " * 2, label + ":", getattr(sample, value))
                if self.options["verbose"] >= required_verbose_level: print(string)
                if file_handle is not None:  file_handle.write("{0}\n".format(string))

        if self.options["verbose"] >= required_verbose_level:  print("")
        if file_handle is not None: file_handle.write("\n")


    def display_information(self, sample, filename = None):
        """
            Displays information about a sample that was returned as part of a search query

            :param sample: Sample object
        """

        identifier = ""
        if sample.type in ["file", "domain"]:
            # INFO: For domains, the identifier is the domain name
            #       This appears to be okay, as for unicode characters an internationalized domain
            #       name is returned which should not cause any conflict with the file system level
            # TODO: check this with dedicated tests
            identifier = sample.id
        elif sample.type == "url":
            identifier = sample.url
        else:
            self.options["auxiliary"].log("Unknown sample type detected: {0} - {1}".format(sample.type, sample.id), level="WARNING")
        print("{0:80}".format(identifier))

        # write the summary information to disk if a filename was provided and the report
        # does not exist yet, otherwise only log but do not rewrite
        file_handle = None
        if (filename is not None) and (not os.path.exists(filename)): 
            file_handle = open(filename, "w")
            file_handle.write("{0}\n".format(identifier))
        elif (filename is not None) and (os.path.exists(filename)):
            self.options["auxiliary"].log("Summary report for sample already exists on disk and is not downloaded again: {0}".format(sample.id), level = "DEBUG")

        if self.options["csv"] and self.options["verbose"] < 3:
            line = ""
            attributes = dir(sample)

            fields = []
            if sample.type == "file":
                fields = ["sha256", "md5", "sha1", "vhash", "size", "type_tag", "tags", "first_submission_date", "last_submission_date", "times_submitted"]
            elif sample.type == "domain":
                fields = ["id", "registrar", "tags", "creation_date", "last_modification_date", "last_update_date"]
            elif sample.type == "url":
                fields = ["url", "last_final_url", "title", "tags", "first_submission_date", "last_submission_date", "times_submitted"]
            else:
                fields = []

            for value in fields: 
                if value not in attributes:
                    line += self.options["separator"]
                    continue

                if isinstance(getattr(sample, value), list):
                    list_items = ""
                    for item in getattr(sample, value):
                        list_items += "{0}|".format(item)
                    line += "\"{0}\"{1}".format(list_items[:-1], self.options["separator"])
                else:
                    line += "\"{0}\"{1}".format(getattr(sample, value), self.options["separator"])

            for value in ["harmless", "malicious", "suspicious", "undetected"]:
                if (("last_analysis_stats" in attributes) and (value in sample.last_analysis_stats.keys())):
                    line += "\"{0}\"{1}".format(sample.last_analysis_stats[value], self.options["separator"])
                else:
                    line += "\"{0}\"".format(self.options["separator"])

            self.options["csv_files"][sample.type].write("{0}\n".format(line[:-1]))
      
        # verbose level 1
        if sample.type == "file":
            values = ["md5", "sha1", "vhash"]
        elif sample.type == "domain":
            values = ["creation_date", "last_modification_date", "last_update_date"]
        elif sample.type == "url":
            values = ["last_final_url", "title"]
        else:
            values = []
        self.display_values(values, sample, required_verbose_level = 1, file_handle = file_handle)
        
        values = ["magic", "type_tag", "tags", "size"]
        self.display_values(values, sample, required_verbose_level = 1, file_handle = file_handle)

        # verbose level 2
        if sample.type in ["file", "url"]:
            values = ["first_submission_date", "last_submission_date", "times_submitted", "unique_sources"]
        elif sample.type == "domain":
            values = ["registrar"]
        else:
            values = []
        self.display_values(values, sample, required_verbose_level = 2, file_handle = file_handle)
   
        values = ["last_analysis_stats"]
        self.display_values(values, sample, ["harmless", "malicious", "suspicious", "undetected"], required_verbose_level = 1, file_handle = file_handle)

        # verbose level 3
        self.display_scanning_results(sample, required_verbose_level = 3, file_handle = file_handle)

        if file_handle is not None: 
            file_handle.close()
            self.options["auxiliary"].log("Saved summary report: {0}".format(filename), level = "DEBUG")


    async def search(self):
        """ Executes a VirusTotal Intelligence search
        """
        
        async with vt.Client(self.options["virustotal"]) as client:
            self.options["auxiliary"].log("Running intelligence query: {0}".format(self.options["query"]))
            it = client.iterator('/intelligence/search',  params={'query': self.options["query"]}, limit=self.options["limit"])
            
            artifact_log = os.path.join(self.options["download_dir"], self.options["filenames"]["artifacts"])

            tasks = []
            asyncio.create_task(self.get_heartbeat())
            with open(artifact_log, "w") as f:
                # iterate through the result set - each element represents a File object
                try:
                    async for obj in it:
                        if obj.type not in ["file", "url", "domain"]:
                            self.options["auxiliary"].log("Warning: Unknown artifact type detected: {0} - {1:70}".format(obj.type, obj.id), level="WARNING")
                            continue
                        
                        # log the name / identifier of the artifact
                        if obj.type in ["file", "domain"]:
                            f.write("{0}\n".format(obj.id))
                        elif obj.type == "url":
                            f.write("{0} => {1}\n".format(obj.id, obj.url)) 

                        # for samples, request downloading the artifact and behavior report
                        if obj.type == "file":
                            if self.options["download_samples"]  : await self.sample_queue.put(obj)
                            if self.options["download_behavior"] : await self.behavior_queue.put(obj)
                        
                        # save the report summary
                        sample_report = os.path.join(self.options["info_dir"], obj.id)
                        self.display_information(obj, sample_report)
                except vt.error.APIError as err:
                    
                    if err.code in ["AuthenticationRequiredError", "ForbiddenError", "UserNotActiveError", "WrongCredentialsError"]:
                        self.auxiliary.log("The API key is not valid for accessing the VirusTotal Private API, or there was a problem with the user account.", level = "ERROR")
                    elif err.code in ["QuotaExceededError", "TooManyRequestsError"]:
                        self.auxiliary.log("The quota for the API key or the number of issued requests has been exceeded.", level = "ERROR")
                    else:
                        self.auxiliary.log("There was an error while processing the request: {0}".format(err.code), level="ERROR")

                    return None

                    
                for worker in range(self.options["workers"]):
                    if self.options["download_behavior"]: tasks.append(asyncio.create_task(self.get_behavior_report()))
                    if self.options["download_samples"]: tasks.append(asyncio.create_task(self.get_sample()))
                            
                await asyncio.gather(*tasks)
                await self.behavior_queue.join()
                await self.sample_queue.join()
                for task in tasks: task.cancel()


    async def execute_request(self, request):
        """ Runs an asynchronous call to retreive a behavioral report from VirusTotal
        
            :param request: The API request to execute

            :return:        JSON output that is contained in the 'data' field
        """

        async with vt.Client(self.options["virustotal"]) as client:
            try:
                url = requests.compat.urljoin(self.site["url"], request)
                result = await client.get_json_async(url)
                
                if "data" not in result:
                    raise ValueError("No valid JSON report received")
                
                return result["data"]
            except vt.error.APIError as err:
                return None
            except ValueError as err:
                self.options["auxiliary"].log("Behavior report for sample did not contain valid data: {0}".format(url))
                return None


    async def get_heartbeat(self):
        """ Periodically print a status message of the queue to indicate the number of pending tasks
        """

        while True:
            sys.stdout.write("\033[94m[Queue] Sample Reports: {0:03d} - Artifacts: {1:03d} - Behavior Reports: {2:03d}\033[0m\r".format(self.info_queue.qsize(), self.sample_queue.qsize(), self.behavior_queue.qsize()))
            sys.stdout.flush()
            await asyncio.sleep(1)


    async def get_behavior_report(self):
        """ Retrieves a behavior report from VirusTotal
            (The behavior report can consist of a result list from multiple sandboxes)

            :return:            True if the report was successfully downloaded or was successfully
                                read from disk (if existing), otherwise False
        """

        async with vt.Client(self.options["virustotal"]) as client:
            while not self.behavior_queue.empty():
                sample = await self.behavior_queue.get()
                sample_id = sample if isinstance(sample, str) else sample.id
                
                # check if a sample object rather than a hash was provided
                report_file = os.path.join(self.options["reports_dir"], sample_id) 
                report_retrieved = False

                # if the report file is not on disk yet, it is downloaded
                if not os.path.isfile(report_file):
                    url = 'files/{0}/behaviours'.format(sample_id)
                    result = await self.execute_request(url)
                    
                    if result is None:
                        self.options["auxiliary"].log("Sample does not have a behavior report, or the report could not be retrieved: {0}".format(sample_id), level="ERROR")
                        self.behavior_queue.task_done()
                        continue
                    try:
                        with open(report_file, "w") as f:
                            json.dump(result, f)
        
                        self.options["auxiliary"].log("Saved behaviorial report: {0}".format(report_file), level = "DEBUG")
                        report_retrieved = True
                    except IOError as err:
                        self.options["auxiliary"].log("Error while saving behaviorial report: {0} - {1}".format(report_file, err), level = "ERROR")
                else:
                    # the report has already been downloaded and is stored on disk
                    self.options["auxiliary"].log("Behavior report for sample already exists on disk and is not downloaded again: {0}".format(sample_id), level = "DEBUG")
            
                    try:  
                        with open(report_file, "r") as f:
                            result = json.load(f)

                        report_retrieved = True
                    except (IOError, json.JSONDecodeError) as err:
                        self.options["auxiliary"].log("Error while reading behaviorial report: {0} - {1}".format(report_file, err), level = "ERROR")

                if report_retrieved:
                    sandbox = Sandbox_Parser(self.options, result)
                    sandbox.parse_report(sample)

                self.behavior_queue.task_done()
                        

    async def download_samples(self, filename):
        """ Reads in a list of hashes from a file for subsequent sample download

            :param filename: The name of the file that contains the list of hashes
        """
        
        md5 = re.compile(r"([a-fA-F\d]{32})")
        sha1 = re.compile(r"([a-fA-F\d]{40})")
        sha256 = re.compile(r"([a-fA-F\d]{64})")

        samples = []
        asyncio.create_task(self.get_heartbeat())
        with open(filename, "r") as f:
            for data in f:
                data = data.strip("\n ")
                if md5.match(data) or sha1.match(data) or sha256.match(data):
                    # if the entry in the file represents a sample by hash, and the 
                    # sample is appearing for the first time, add it to the queue
                    if data not in samples:
                        await self.info_queue.put(data)
                        samples.append(data)

        # retrieve summary information and check if the sample exists
        tasks = []
        for worker in range(self.options["workers"]):
            result = tasks.append(asyncio.create_task(self.get_sample_info()))
                    
        results = await asyncio.gather(*tasks)
        await self.info_queue.join()
        for task in tasks: task.cancel()

        # download artifacts that are existing as well as corresponding behavior reports
        for worker in results:
            for sample in worker:
                if sample is not None:
                    if self.options["download_samples"]  : await self.sample_queue.put(sample)
                    if self.options["download_behavior"] : await self.behavior_queue.put(sample)

        tasks = []
        for worker in range(self.options["workers"]):
            if self.options["download_behavior"]: tasks.append(asyncio.create_task(self.get_behavior_report()))
            if self.options["download_samples"]: tasks.append(asyncio.create_task(self.get_sample()))
                    
        await asyncio.gather(*tasks)
        await self.behavior_queue.join()
        await self.sample_queue.join()
        for task in tasks: task.cancel()


    async def get_sample_info(self):
        """ Retrieves summary information about a sample
        """
        
        samples = []
        async with vt.Client(self.options["virustotal"]) as client:
            while not self.info_queue.empty():
                try:
                    sample_id = await self.info_queue.get()
                    path = os.path.join("/files", sample_id)
                    
                    # this call should be always performed to check if the sample exists
                    # and get context information for a hash value
                    result = await client.get_object_async(path)

                    sample_report = os.path.join(self.options["info_dir"], sample_id)
                    self.display_information(result, sample_report)

                    samples.append(result)
                except vt.error.APIError as err:
                    if err.code == "NotFoundError":
                        self.options["auxiliary"].log("Sample was not found: {0}\n".format(sample_id), level = "WARNING")
                        continue
                    elif err.code in ["AuthenticationRequiredError", "ForbiddenError", "UserNotActiveError", "WrongCredentialsError"]:
                        self.auxiliary.log("The API key is not valid for accessing the VirusTotal Private API, or there was a problem with the user account.", level = "ERROR")
                    elif err.code in ["QuotaExceededError", "TooManyRequestsError"]:
                        self.auxiliary.log("The quota for the API key or the number of issued requests has been exceeded.", level = "ERROR")
                    else:
                        self.auxiliary.log("There was an error while processing the request: {0}".format(err.code), level="ERROR")
                    
                    # clear all remaining items in the queue
                    while not self.info_queue.empty(): 
                        await self.info_queue.get()
                        self.info_queue.task_done()

                self.info_queue.task_done()

        return samples


    async def get_sample(self):
        """ Downloads a sample from VirusTotal

            :param sample_id:   The id (hash value) of the sample
            
            :return:            True if the sample was successfully downloaded, otherwise False
                                (In case the sample already exists on disk, the return value
                                is also False)
        """
        
        async with vt.Client(self.options["virustotal"]) as client:
            while not self.sample_queue.empty():
                try:
                    sample_id = await self.sample_queue.get()
                    # check if a sample object rather than a hash was provided
                    if not isinstance(sample_id, str): sample_id = sample_id.id
                    
                    sample_path = os.path.join(self.options["samples_dir"], sample_id)
                    
                    # if the file is already on disk, it is not downloaded again
                    # TODO: Possibly check more than purely the filename to be sure the content was previously
                    #       correctly downloaded as well?  
                    if os.path.isfile(sample_path): 
                        self.options["auxiliary"].log("Sample already exists on disk and is not downloaded again: {0}".format(sample_id), level = "DEBUG")
                        self.sample_queue.task_done()
                        continue
                    
                    # save the sample to disk
                    with open(sample_path, "wb") as f:
                        await client.download_file_async(sample_id, f)
                        self.options["auxiliary"].log("Successfully downloaded sample: {0}".format(sample_id), level = "DEBUG")

                    self.sample_queue.task_done()
                except IOError as err:
                    self.options["auxiliary"].log("Error while downloading sample: {0}".format(err), level = "ERROR")
                    self.sample_queue.task_done()
