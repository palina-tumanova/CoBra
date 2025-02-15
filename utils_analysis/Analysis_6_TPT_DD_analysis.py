import os
import json
import time
import numpy as np
import csv
from matplotlib import pyplot as plt
# path = os.getcwd() + '/result/Scenario_1_2800/Run_2/last_runs_02-15-2023_08-54-08_EDD.json'
path = os.getcwd() + '/result/last_runs/last_runs_02-15-2023_15-31-57.json'
# path = os.getcwd() + '/result/EDD_Prio_relation/last_runs_02-15-2023_18-17-04_RL copy.json'

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
lenghts_low_load = [26.64, 21.55,13.81,18.93]
load_constant = 40
product_types = ["Produkt A","Produkt B","Produkt C","Produkt D"]
throughput_times_1 = []
throughput_times_2 = []
throughput_times_3 = []
throughput_times_avg = []
throughput_time_avg_k = 0
moving_average = 100
for m in priorities:
    prio_data = []
    throughput_times_avg = None
    throughput_times_avg = []
    throughput_times_1 = []
    throughput_times_2 = []
    throughput_times_3 = []
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
        completion_time = data["orders"][0]["orders"][i]["item_results"]["completion_time"]
        due_to_time = data["orders"][0]["orders"][i]["due_to"]-data["orders"][0]["orders"][i]["start"]
        priority = data["orders"][0]["orders"][i]["priority"]
        throughput_time = data["orders"][0]["orders"][i]["item_results"]["transportation_time"]+data["orders"][0]["orders"][i]["item_results"]["time_at_machines"]+data["orders"][0]["orders"][i]["item_results"]["time_in_interface_buffer"]+data["orders"][0]["orders"][i]["item_results"]["time_in_queue_buffer"]+data["orders"][0]["orders"][i]["item_results"]["wait_for_repair_time"]
        if due_to_time == 0:
            tardiness = data["orders"][0]["orders"][i]["start"]  + throughput_time - length - data["orders"][0]["orders"][i]["due_to"] 
        else:
            tardiness = data["orders"][0]["orders"][i]["start"]  + throughput_time - length - data["orders"][0]["orders"][i]["due_to"] 
        lateness = tardiness
        if tardiness < 0:
            tardiness = 0
            proc_in_time = 1
        if completion_time != 0:
            if data["orders"][0]["orders"][i]["priority"] == m:
                    # if due_to_time == 0:
                    #     throughput_times_1.append([throughput_time, tardiness])
                    # elif due_to_time >= 30 and due_to_time <= 45:
                    #     throughput_times_2.append([throughput_time, tardiness])
                    # elif due_to_time > 45 and due_to_time <= 60:
                    #     throughput_times_3.append([throughput_time, tardiness])
                    if due_to_time == 0:
                        throughput_times_1.append([throughput_time])
                    else:
                        throughput_times_2.append([throughput_time])
                    
    # print("prio: ", m, ", mean: ", np.mean(throughput_times_1, axis = 0), " , len: ",len(throughput_times_1))
    # print("prio: ", m, ", mean: ", np.mean(throughput_times_2, axis = 0), " , len: ",len(throughput_times_2))
    print(m, ", ", np.mean(throughput_times_1, axis = 0) , ", " ,len(throughput_times_1))
    print(m, ", ", np.mean(throughput_times_2, axis = 0), ", " ,len(throughput_times_2))
    # print("prio: ", m, ", mean: ", np.mean(throughput_times_3, axis = 0), " , len: ",len(throughput_times_3))                    