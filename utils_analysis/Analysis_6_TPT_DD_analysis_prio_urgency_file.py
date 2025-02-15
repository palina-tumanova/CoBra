import os
import json
import time
import numpy as np
import csv
from matplotlib import pyplot as plt

# os.chdir("..")
# path = os.getcwd() + '/result/last_runs/load_analysis/Run_3/last_runs_04-17-2023_16-39-03.json'
path = os.getcwd() + '/result/last_runs/last_runs_04-21-2023_10-58-42.json'
# path = os.getcwd() + '/result/last_runs_04-12-2023_09-49-15.json'

numbers = open(path)
data = json.load(numbers)

# count files in directory that needs to be analyzed
json_file_count = 0 
for roots,dirs, files in os.walk(os.getcwd() + '/Benchmark/Data'):
    for file in files:
        if os.path.splitext(file)[1] == '.json':
            json_file_count += 1   

prio_data = []
length = 0
proc_in_time = 0
priorities = [0,1]
urgencies = [0,1]
lenghts_low_load = [21.4, 22.0,26.4,28.1]
load_constant = 40
product_types = ["Produkt A","Produkt B","Produkt C","Produkt D"]
throughput_times = []
tardiness_times = []
throughput_times_avg = []
throughput_time_avg_k = 0
moving_average = 100
print("Number of orders: " , range(len(data["orders"][0]["orders"])))
for m in priorities:
    for u in urgencies:
        prio_data = []
        throughput_times_avg = None
        throughput_times_avg = []
        throughput_times = []
        tardiness_times = []
        proc_in_time = 0
        priority, tardiness, lateness, completion_time, throughput_time, time_to_EDD = 0,0,0,0,0,0
        for i in range(len(data["orders"][0]["orders"])):
            proc_in_time = 0
            type = data["orders"][0]["orders"][i]["type"]
            if type == "Produkt A":
                length = lenghts_low_load[0] + load_constant
            elif type == "Produkt B":
                length = lenghts_low_load[1] + load_constant
            elif type == "Produkt C":
                length = lenghts_low_load[2] + load_constant
            elif type == "Produkt D":
                length = lenghts_low_load[3] + load_constant
            
            if u == 1:
                length = length + 45

            completion_time = data["orders"][0]["orders"][i]["item_results"]["completion_time"]
            due_to_time = data["orders"][0]["orders"][i]["due_to"]-data["orders"][0]["orders"][i]["start"]
            priority = data["orders"][0]["orders"][i]["priority"]
            throughput_time = data["orders"][0]["orders"][i]["item_results"]["transportation_time"]+data["orders"][0]["orders"][i]["item_results"]["time_at_machines"]+data["orders"][0]["orders"][i]["item_results"]["time_in_interface_buffer"]+data["orders"][0]["orders"][i]["item_results"]["time_in_queue_buffer"]+data["orders"][0]["orders"][i]["item_results"]["wait_for_repair_time"]
            tardiness = data["orders"][0]["orders"][i]["start"]  + throughput_time - length - data["orders"][0]["orders"][i]["due_to"] 

            # if completion_time != 0 and data["orders"][0]["orders"][i]["priority"] == 1 and data["orders"][0]["orders"][i]["urgency"] == 0: 
            #     print(i, ", ",m, ", ",u, ", ", throughput_time)

            lateness = tardiness
            if tardiness < 0:
                tardiness = 0
                proc_in_time = 1
            if completion_time != 0:
                if data["orders"][0]["orders"][i]["priority"] == m and data["orders"][0]["orders"][i]["urgency"] == u:
                    throughput_times.append([throughput_time])
                    tardiness_times.append([tardiness])
            
        print(m,",", u,", throughput_time    , ", round(np.mean(throughput_times, axis = 0)[0],1), ", tardiness, ", round(np.mean(tardiness_times, axis = 0)[0],1), ", " ,len(throughput_times))
        