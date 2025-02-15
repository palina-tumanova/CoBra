from ast import Or
from logging.config import valid_ident
from objects.machines import Machine
from objects.buffer import *
from objects.rulesets import RuleSet
from configs.models import ReinforceAgent
from utils.consecutive_performable_tasks import consecutive_performable_tasks
from utils.devisions import div_possible_zero
from configs.dict_pos_types import dict_pos_types
import pandas as pd
import numpy as np
from copy import copy
import random
import csv
import os
import math
import time
from utils import time_tracker, reward_layer


def set_agent_seed(seed:int):
    random.seed(seed)


class ManufacturingAgent:
    instances = []

    def __init__(self, config: dict, env: simpy.Environment, position, ruleset_id=None):

        self.env = env
        self.simulation_environment = None
        self.init_time = time.time()
        t = time.localtime()
        self.timestamp = time.strftime('_%Y-%m-%d_%H-%M-%S', t)
        self.lock = None
        self.count = 0
        self.count_smart = 0
        self.global_step = 0
        self.init_pos = True
        
        # Attributes
        self.ruleset = None
        self.smart_init = True
        self.smart_agent = None
        self.smart_id = None

        self.operational_mode = False 
        if ruleset_id == 10:
            self.smart_id = 10
        elif ruleset_id == 11:
            self.smart_id = 10
            self.operational_mode = True #operational mode activated

        for ruleset in RuleSet.instances:
            if ruleset.id == ruleset_id:
                self.ruleset = ruleset
                # Reference to the priority ruleset of the agent
                break

        self.state_cols = ["due_to", "priority", "time_in_cell", "start", "distance", "urgency"]
        self.smart_dispatch_rules =  [1,2,3,4,9]
        self.smart_dynamic_dispatch = False
        if ruleset_id != 10:
            self.ranking_criteria = [criteria["measure"] for criteria in self.ruleset.numerical_criteria]
            if not self.ruleset:  # Check if the Agent has a Ruleset selected
                raise Exception(
                    "Atleast one Agent has no ruleset defined. Please choose a ruleset or the agent wont do anything!")
    
        self.ranking_criteria_assist = None
        self.ruleset_temp = None

        self.cell = None

        #for distribution and manufacturing cell 
        self.distribution_simple = True
        self.distribution_opt = False
        self.distribution_smart = False
        
        self.speed = config["AGENT_SPEED"]  # Configured moving speed of the agent: How much distance can be moved within one time points
        self.time_for_item_pick_up = config["TIME_FOR_ITEM_PICK_UP"]
        self.time_for_item_store = config["TIME_FOR_ITEM_STORE"]

        # State
        self.moving = False  # Is the agent currently moving from one position to another?
        self.position = position  # Position object of the agent, None if agent is currently moving
        self.next_position = None  # Destination if agent is currently moving
        self.moving_time = 0  # How long does it take the agent to perform the whole route
        self.moving_start_time = None  # When did the agent start moving
        self.moving_start_position = None  # Where did the agent start moving
        self.remaining_moving_time = 0  # How much moving time of the current route is remaining
        self.moving_end_time = None  # Estimated Time point on which the agent will arrive

        self.waiting = False  # Agent has an active waiting task, only interruptable by the position or after a specific time passed (LONGEST_WAITING_TIME)
        self.has_task = False  # Has the agent an active task it performs? Waiting counts as task...

        self.locked_item = None  # Item locked by this agent. Locked items are not interactable by other agents
        self.picked_up_item = None  # Item the agent is holding, only one at a time

        self.started_tasks = 0  # Amount of started tasks

        # Current tasks
        self.current_task = None  # The current task the agent 0.6:
        self.current_subtask = None  # Current subtask the agent is performing (Subtasks are part of the current task e.g. "move to position x" as part of "bring item y from z to x")
        self.current_waitingtask = None  # Current waiting task. Agents starts waiting task if its subtask/task cant be performed currently (e.g. wait for processing of item in machine)

        self.__class__.instances.append(self)
        self.env.process(self.initial_event())  # Write initial event in event log when simulation starts
        self.main_proc = self.env.process(self.main_process())  # Initialize first main process of the agent when simulation starts

    def main_process(self):
        """Main process of the agent. Decisions about its behavior are made in this process.
        Will call Tasks after calculating the next task to be done.
        """

        if not self.cell.orders_available():
            return
        self.lock.acquire()
        self.global_step += 1
        len_useable_with_free_destination = 0
        # Get state of cell and orders inside this cell
        state_calc_start = time.time()
        cell_state = self.cell.get_cell_state(requester=self)  
        time_tracker.time_state_calc += time.time() - state_calc_start

        # For each order in state add the destination if this order would be chosen
        dest_calc_start = time.time()
        cell_state["_destination"] = cell_state.apply(self.add_destinations, axis=1) 
        time_tracker.time_destination_calc += time.time() - dest_calc_start   
        # self.ranking_criteria = [criteria["measure"] for criteria in self.ruleset.numerical_criteria]
        
        # set initial smart agent if smart ruleset is chosen by using a unique cell identifier
        if self.smart_init == True and self.smart_id == 10:
            self.set_deep_agent(cell_state)

        # Get action depending on agent ruleset and cell_state
        if self.ruleset.dynamic_dispatch or self.smart_dynamic_dispatch:
            now = time.time()
            next_task, next_order, destination, base_state, state_RL, action, action_RL, len_useable_with_free_destination = self.get_smart_dispatch_rule(cell_state)
            time_tracker.time_smart_action_calc += time.time() - now
        else:
            now = time.time()
            next_task, next_order, destination, base_state, len_useable_with_free_destination = self.get_action(cell_state)
            time_tracker.time_action_calc += time.time() - now
            time_tracker.action_normal += 1
            action = self.get_heuristics_action_index(cell_state, next_order)

        # save actions for later analysis
        # if self.cell.id == 5:
        #     self.save_action(cell_state, action, next_order)

        # Perform next task if there is one~
        if next_task:
            self.current_task = next_task
            self.has_task = True
            self.save_event("start_task")
            self.started_tasks += 1

            task_started_at = self.env.now

            # Lock next order
            if next_order:
                next_order.locked_by = self
                self.locked_item = next_order
                self.locked_item.save_event("locked")
                self.announce_arrival(next_order, destination)

            self.lock.release()

            # Perform task
            yield next_task

            self.has_task = False
            self.save_event("end_of_main_process")

            state_calc_start = time.time()
            # Get new state
            new_cell_state = self.cell.get_cell_state(requester=self)
            time_tracker.time_state_calc += time.time() - state_calc_start
            dest_calc_start = time.time()
            new_cell_state["_destination"] = new_cell_state.apply(self.add_destinations, axis=1)
            time_tracker.time_destination_calc += time.time() - dest_calc_start
            new_cell_state = self.state_to_numeric(copy(new_cell_state))

            if len_useable_with_free_destination > 1:
                if not self.ruleset.dynamic and not self.ruleset.dynamic_dispatch:
                    self.finished_heuristic_action(cell_state, new_cell_state, base_state, next_order, self.env.now - task_started_at, action, len_useable_with_free_destination)

                if (self.ruleset.dynamic or self.ruleset.dynamic_dispatch) and action_RL != None:  # Check rewards            
                    self.finished_smart_action(cell_state, new_cell_state, base_state, state_RL, next_order, action, action_RL, len_useable_with_free_destination)

            # Start new main process
            self.main_proc = self.env.process(self.main_process())

        if self.lock.locked():
            self.lock.release()

    def set_deep_agent(self, cell_state):
        cell_type = self.cell.type
        cell_level = self.cell.level
        smart_state_len = len(self.state_cols)*len(cell_state)
        smart_action_len = len(self.smart_dispatch_rules)
        input_buffer_capacity = self.cell.input_buffer.storage_capacity
        output_buffer_capacity = self.cell.output_buffer.storage_capacity
        storage_capacity = self.cell.storage.storage_capacity
        agent_count = len(self.cell.agents)
        identifier = f"{cell_type}_{cell_level}_{smart_state_len}_{smart_action_len}_{input_buffer_capacity}_{output_buffer_capacity}_{storage_capacity}_{agent_count}"
        if cell_type == "Dist":
            for i in range(len(self.cell.childs)):
                input_buffers = self.cell.childs[i].input_buffer.storage_capacity
                output_buffers = self.cell.childs[i].output_buffer.storage_capacity
                identifier += f"_{input_buffers}_{output_buffers}"
        else:  
            identifier += f"_{len(self.cell.machines)}"
    
        self.smart_agent = ReinforceAgent(smart_state_len, smart_action_len, self.operational_mode, identifier,self.cell.level)
        self.smart_init = False
        self.smart_dynamic_dispatch = True
        # time.sleep(10)

    def get_action(self, order_state): #get action with neural network but plain dispatch rules
        """Gets an action by using the priority attributes defined in agents ruleset
        :param order_state: Pandas Dataframe, categorical state of the cell
        :return task: simpy process to be performed next
        :return next_order: order to be moved
        :return destination: destination where the order will be brought to""" 
        state_numeric = self.state_to_numeric(copy(order_state))

        order = order_state[(order_state["order"].notnull())]
        useable_orders = order[(order["locked"] == 0) & (order["in_m_input"] == 0) & (order["in_m"] == 0) & (order["in_same_cell"] == 1)] 

        if useable_orders.empty:
            return None, None, None, None, None
        
        useable_with_free_destination = useable_orders[useable_orders["_destination"] != -1]
        if useable_with_free_destination.empty:
            return None, None, None, None, None

        elif len(useable_with_free_destination) == 1:
            next_order = useable_with_free_destination["order"].iat[0]

        elif self.ruleset.random:  # When Ruleset is random...
            ranking = useable_with_free_destination.sample(frac=1, random_state=self.ruleset.seed).reset_index(
                drop=True)
            next_order = ranking["order"].iat[0]

        else:
            criteria = [criteria["measure"] for criteria in self.ruleset.numerical_criteria]
            ranking = useable_with_free_destination.loc[:, ["order"] + criteria]

            for criterion in self.ruleset.numerical_criteria: #Place where heuristics apply
                weight = criterion["weight"]
                measure = criterion["measure"]
                order = criterion["ranking_order"]

                max_v = ranking[measure].max()
                min_v = ranking[measure].min()

                # Min Max Normalisation
                if order == "ASC":
                    ranking["WS-" + measure] = weight * div_possible_zero((ranking[measure] - min_v), (max_v - min_v))
                else:
                    ranking["WS-" + measure] = weight * (1 - div_possible_zero((ranking[measure] - min_v), (max_v - min_v)))

            order_scores = ranking.filter(regex="WS-")
            ranking.loc[:, "Score"] = order_scores.sum(axis=1)
            ranking.sort_values(by=["Score"], inplace=True)

            next_order = ranking["order"].iat[0]
            

        destination = useable_with_free_destination[useable_with_free_destination["order"] == next_order].reset_index(drop=True).loc[0, "_destination"]

        if destination:
            return self.env.process(self.item_from_to(next_order, next_order.position, destination)), next_order, destination, state_numeric, len(useable_with_free_destination)
        else:
            return None, None, None, None, None

    def get_RL_state(self, order_state, available_destinations): 
        try:
            state = order_state[self.state_cols]
        except:
            print("State error")  
            state = pd.DataFrame(np.zeros((len(order_state), len(self.state_cols))))

        state_order_priority = np.multiply(available_destinations, state["priority"])
        state_urgency = np.multiply(available_destinations, state["priority"])
        state_distance = np.multiply(available_destinations, state["distance"])/np.max(state["distance"])

        current_sim_time = self.env.now
        # a = np.empty(len(available_destinations)); a.fill(current_sim_time)

        state_due_to_available = np.multiply(available_destinations, (state["due_to"]))
        time_in_cell_available = np.multiply(available_destinations, (state["time_in_cell"]))
        time_in_system_available = np.multiply(available_destinations, (state["start"]))

        max_time_in_cell = max(i for i in time_in_cell_available)
        max_time_in_system = max(i for i in time_in_system_available)
        max_due_to = max(i for i in state_due_to_available)

        min_time_in_cell = min(i for i in time_in_cell_available)
        min_time_in_system = min(i for i in time_in_system_available)
        min_due_to = min(i for i in state_due_to_available)

        state_due_to_available_normalized, state_due_to_normalized = np.zeros(len(state_due_to_available)), np.zeros(len(state_due_to_available))
        time_in_cell_available_normalized, state_time_in_cell_normalized = np.zeros(len(time_in_cell_available)), np.zeros(len(time_in_cell_available))
        time_in_system_available_normalized, state_time_in_system_normalized = np.zeros(len(time_in_system_available)), np.zeros(len(time_in_cell_available))

        if min_due_to != 0:
            state_due_to_normalized = state_due_to_available / max_due_to
            state_due_to_available_normalized = (2*(max_due_to - state_due_to_available)/(max_due_to - min_due_to)) - 1
            
        if max_time_in_cell != 0:
            state_time_in_cell_normalized = time_in_cell_available / max_time_in_cell
            time_in_cell_available_normalized = 1 - 2*(max_time_in_cell-time_in_cell_available)/(max_time_in_cell - min_time_in_cell)
            
        if max_time_in_system != 0:
            state_time_in_system_normalized = time_in_system_available / max_time_in_system
            time_in_system_available_normalized = 1 - 2*(max_time_in_system - time_in_system_available)/(max_time_in_system - min_time_in_system)
        
        # only keep correct values and not -1 values from subtraction
        state_due_to_available_normalized = np.multiply(available_destinations, state_due_to_available_normalized)
        time_in_cell_available_normalized = np.multiply(available_destinations, time_in_cell_available_normalized)
        time_in_system_available_normalized = np.multiply(available_destinations, time_in_system_available_normalized)
    
        state_RL = np.vstack((state_due_to_available_normalized, time_in_cell_available_normalized, 
                            time_in_system_available_normalized, state_distance, state_order_priority, state_urgency)).T.ravel().tolist()

        return state_RL

    def get_available_destinations(self, order_state): #get numerized _destination space
        destination = order_state.loc[:, "_destination"]
        available_destinations = []
        for i in range(len(destination)): #(2) look for orders on valid places
            if destination[i] == -1:
                available_destinations.append(0)
            else:
                available_destinations.append(1)
        return available_destinations

    def get_smart_dispatch_rule(self, order_state):
        """Gets an action by using an dynamic reinforcement learning model defined in agents ruleset
        :param order_state: Pandas Dataframe, categorical state of the cell
        :return task: simpy process to be performed next
        :return next_order: order to be moved
        :return destination: d estination where the order will be brought to"""
        
        if self.smart_id == 10:
            smart_agent = self.smart_agent
        else:
            smart_agent = self.ruleset.reinforce_agent
        
        state_numeric = self.state_to_numeric(copy(order_state))
        available_destinations = self.get_available_destinations(state_numeric)
        order = order_state[(order_state["order"].notnull())]
        useable_orders = order[(order["locked"] == 0) & (order["in_m_input"] == 0) & (order["in_m"] == 0) & (order["in_same_cell"] == 1)]

        if useable_orders.empty:
            return None, None, None, None, None, None, None, None
        useable_with_free_destination = useable_orders[useable_orders["_destination"] != -1]

        orders_in_machine = order[(order["in_m_input"] == 1) & (order["in_m"] == 1) & (order["in_same_cell"] == 1)]

        if useable_with_free_destination.empty:
            return None, None, None, None, None, None, None, None
        elif len(useable_with_free_destination) == 1:
            next_order = useable_with_free_destination["order"].iat[0]
            state_RL, action_RL = None, None
        else:
            time_tracker.action_smart += 1
            possible_dispatch_rules = self.smart_dispatch_rules
            state_RL = self.get_RL_state(state_numeric, available_destinations)
            # if order threshold is reached, control might be taken over by heuristics
            current_threshold = (len(self.cell.orders_in_cell)-len(orders_in_machine))/(self.cell.cell_capacity - 2*len(self.cell.machines))           
            action_RL = smart_agent.get_dispatch_rule(state_RL) #smart agent thinking...
            for ruleset in RuleSet.instances:
                if ruleset.id == possible_dispatch_rules[action_RL]:
                    self.ruleset_temp = ruleset # Reference to the choosen ruleset of the smart agent
                    break
     
            self.ranking_criteria_assist = [criteria["measure"] for criteria in self.ruleset_temp.numerical_criteria]
            criteria = [criteria["measure"] for criteria in self.ruleset_temp.numerical_criteria]

            if self.ruleset_temp.id == 9 and (len(useable_with_free_destination[useable_with_free_destination["priority"] == 2]) > 1 or len(useable_with_free_destination[useable_with_free_destination["priority"] == 1]) > 1) : #pre-sorting for due_to rule
                for ruleset in RuleSet.instances:           
                    if ruleset.id == 4:
                        ruleset_due_to = ruleset # Reference to the choosen ruleset of the smart agent
                        break
                self.ranking_criteria_assist = [criteria["measure"] for criteria in ruleset_due_to.numerical_criteria]
                criteria_temp = [criteria["measure"] for criteria in ruleset_due_to.numerical_criteria]
                ranking = useable_with_free_destination.reindex(columns = (["order"] + criteria_temp + criteria))
                for criterion in ruleset_due_to.numerical_criteria:
                    weight = criterion["weight"]
                    measure = criterion["measure"]
                    order = criterion["ranking_order"]
                    max_v = ranking[measure].max()
                    min_v = ranking[measure].min()

                    # Min Max Normalisation
                    if order == "ASC":
                        ranking["WS-" + measure] = weight * div_possible_zero((ranking[measure] - min_v), (max_v - min_v))
                    else:
                        ranking["WS-" + measure] = weight * (1 - div_possible_zero((ranking[measure] - min_v), (max_v - min_v)))
                order_scores = ranking.filter(regex="WS-")
                ranking.loc[:, "Score"] = order_scores.sum(axis=1)
                ranking.sort_values(by=["Score"], inplace=True)
                ranking.drop(columns=["Score"])
            else:
                ranking = useable_with_free_destination.reindex(columns = (["order"] + criteria))

            for criterion in self.ruleset_temp.numerical_criteria:
                weight = criterion["weight"]
                measure = criterion["measure"]
                order = criterion["ranking_order"]

                max_v = ranking[measure].max()
                min_v = ranking[measure].min()

                # Min Max Normalisation
                if order == "ASC":
                    ranking["WS-" + measure] = weight * div_possible_zero((ranking[measure] - min_v), (max_v - min_v))
                else:
                    ranking["WS-" + measure] = weight * (1 - div_possible_zero((ranking[measure] - min_v), (max_v - min_v)))

            order_scores = ranking.filter(regex="WS-")
            ranking.loc[:, "Score"] = order_scores.sum(axis=1)
            ranking.sort_values(by=["Score"], inplace=True)
            next_order = ranking["order"].iat[0]
        
        action = self.get_heuristics_action_index(order_state, next_order)
        # action_next_order = order_state[order_state["order"]==next_order].index.values
        # action = action_next_order

        destination = useable_with_free_destination[useable_with_free_destination["order"] == next_order].reset_index(drop=True).loc[0, "_destination"]
        # next_task, next_order, destination, base_state_flat, action, dynamic_temp
        if destination:
            return self.env.process(self.item_from_to(next_order, next_order.position, destination)), next_order, destination, state_numeric, state_RL, action, action_RL, len(useable_with_free_destination)
        else:
            return None, None, None, None, None, None, None, None

    def get_heuristics_action_index(self, order_state, next_order):
        action_next_order = order_state[order_state["order"]==next_order].index.values
        return action_next_order

    def get_RL_action_index(self, action): #find corresponding action
        valid_actions = []
        if self.ruleset.id == 10:
            valid_actions = [1,2,3,4,9] #(1) positions in whole state vector first column that are valid
        valid_actions.append(valid_actions[len(valid_actions)-1] + 1)
        action_RL = 0
        i = 0
        for i in range(len(valid_actions)):
            if valid_actions[i] == action:
                action_RL = i
                return action_RL

    def finished_heuristic_action(self, old_state, new_state, old_state_flat, order, time_passed, action, len_usable_orders): # calc reward and append memory
        # new_state_flat = list(self.state_to_numeric(copy(new_state)).to_numpy().flatten())
        self.count = self.count + 1
        processable_orders = self.get_processable_orders(old_state)
        priority = old_state.loc[action, "priority"].values[0]
        distance = old_state.loc[action, "distance"].values[0]
        urgency = old_state.loc[action, "urgency"].values[0]

        if len_usable_orders > 1:
            reward = reward_layer.reward_smart_dispatch(old_state, new_state, order, action, self.cell.type)
            agent_name = str(self)
            agent_name = agent_name[-14:-1]
            parent = str(self.cell.parent)
            try:
                parent = parent[-14:-1]
            except:
                parent = None
            if self.cell.id == 8:
                with open('../result/rewards/rewards' + self.timestamp + '_' + "cell.id-" + str(self.cell.id) + '_agent-' + agent_name + '_level-' + str(self.cell.level) + '_parent-' + parent + '_rule-' + str(self.ruleset.id) +  '.csv', 'a+', newline='', encoding='utf-8') as file:
                    writer = csv.writer(file)
                    writer.writerow(list([self.cell.id, self.ruleset.id, agent_name, self.count, round(reward,2), priority, urgency, round(distance,2), len_usable_orders, action]))
        
        

    def finished_smart_action(self, old_state, new_state, old_state_flat, state_RL, order, action, action_RL, len_usable_orders): # calc reward and append memory
        """Calculate reward for smart action and inform reinforcement agent about the state changes
        :param old_state: The state at action decision (categorical)
        :param new_state: Current state after finished task (categorical)
        :param old_state_flat: Flat state at action decision (numeric)
        :param order: (Order object) The moved order from finished taskc
        :param time_passed: (float) Time passed between action decision and finished task
        :param action: (int) The chosen action""" 
        if self.smart_id == 10:
            smart_agent = self.smart_agent
        else:
            smart_agent = self.ruleset.reinforce_agent
        # smart_agent = self.ruleset.reinforce_agent
        available_destinations = self.get_available_destinations(new_state)
        new_state_RL = self.get_RL_state(new_state, available_destinations)
        # new_state_flat = list(self.state_to_numeric(copy(new_state)).to_numpy().flatten())        
        self.count_smart = self.count_smart + 1
        priority = old_state.loc[action, "priority"].values[0]
        distance = old_state.loc[action, "distance"].values[0]
        urgency = old_state.loc[action, "urgency"].values[0]

        if len_usable_orders  > 1: #if more than one order was apparent. 0,1: no AI necessary
            agent_name = str(self)
            agent_name = agent_name[-14:-1]
            parent = str(self.cell.parent)
            try:
                parent = parent[-14:-1]
            except:
                parent = None

            reward = reward_layer.reward_smart_dispatch(old_state, new_state, order, action, self.cell.type)

            with open('../result/rewards/rewards_' + self.timestamp + '_level_' + str(self.cell.level) + '_cell.id-' + str(self.cell.id) + '_agent-' + agent_name +  '_level-' + '_parent-' + parent + '_rule-' + str(self.ruleset.id) +  '.csv', 'a+', newline='', encoding='utf-8') as file:
                writer = csv.writer(file)
                writer.writerow(list([self.cell.id, self.ruleset.id, agent_name, self.count_smart, round(reward,0), priority, urgency, round(distance,2), len_usable_orders, action, action_RL]))
            smart_agent.appendMemory(smart_agent, self.cell.id, state_RL, new_state_RL, action_RL, reward)

    def get_processable_orders(self, old_state):
            # print("orders: ", len(old_state["order"]) - sum(x is not None for x in old_state["order"]), "; ", reward)
            old_cell_state_due_to = old_state.loc[:, "due_to"]

            #get due_to values for all orders that have a destination
            destination = old_state.loc[:, "_destination"]
            available_destinations = []
            for i in range(len(destination)): #(2) look for orders on valid places, if not valid; nan
                if destination[i] == -1:
                    available_destinations.append(np.nan)
                else:
                    available_destinations.append(1)
            due_to_values = np.multiply(old_cell_state_due_to, available_destinations)
            relevant_due_tos = [x for x in due_to_values if np.isnan(x) == False]
            return len(relevant_due_tos)
        

    def add_destinations(self, data):
        """Takes an column of a state and add the destination where the order would go to if chosen by the agent:
        :param data: (pd.Series) Column of state
        :return destination: Calculated destination or -1 if no useable order or no available destination for that order"""

        # useable_order = (pd.notnull(data["order"])) and (data["locked"] == 0) and (data["in_m_input"] == 0) and (data["in_m"] == 0) and (data["picked_up"] ==  0)

        if self.cell.machines:
            useable_order = (pd.notnull(data["order"])) and (data["locked"] == 0) and (data["in_m_input"] == 0) and (data["in_m"] == 0)
        else:
            useable_order = (pd.notnull(data["order"])) and (data["locked"] == 0) and (data["in_m_input"] == 0) and (data["in_m"] == 0) # and (data["interface_ingoing"] == 1)

        if useable_order:
            destination = self.calculate_destination(data["order"])

            if destination:
                return destination

        return -1

    def calculate_destination(self, order):
        """Calculate the best next position for an agent to bring an order

        :param order: (Order object) Item to be calculated
        :return destination: Machine or Buffer within the Cell where order would be brought to. None if no destination is available
        """
        destination = None
        # if order.current_cell is not self.cell:
        #     return destination
        next_processing_step = order.next_task
        next_steps = order.remaining_tasks

        #Prio1: if order is fininshed bring it to output or cell storage to prevent machine bloackage
        if order.tasks_finished or next_processing_step not in [task for (task, amount) in self.cell.performable_tasks
                                                                if amount > 0]:

            if self.cell.output_buffer.free_slots():
                destination = self.cell.output_buffer
            elif self.cell.storage.free_slots():
                destination = self.cell.storage
            else:
                return None

        elif self.cell.machines:
            # Order is in machine cell

            # Machines in cell that can perform the next task of the order
            potential_machines = [(
                                  machine, machine.item_in_input, machine.item_in_machine, len(machine.expected_orders),
                                  machine.current_setup) for machine in self.cell.machines if
                                  next_processing_step == machine.performable_task]

            # Machines that have a free input and are already in the right setup for the order
            optimal_machines = [machine for (machine, item_input, item_machine, expected_orders, setup) in
                                potential_machines if
                                item_input is None and expected_orders == 0 and setup == order.type]

            if len(optimal_machines) > 0:
                # Prefer one of the optimal machines
                destination = optimal_machines[0]

            else:

                # Possible machines that have a free input slot
                free_machines = [machine for (machine, item_input, item_machine, expected_orders, setup) in
                                 potential_machines if item_input is None and expected_orders == 0]

                # Prefer one of the free machines
                if len(free_machines) > 0:
                    destination = free_machines[0]

            if destination is None:

                # No machine could be used
                if self.cell.storage.free_slots() and order.position is not self.cell.storage:
                    # Bring order to storage buffer if a free slot is available
                    destination = self.cell.storage

                
                # elif self.cell.output_buffer.free_slots() and order.position is not self.cell.output_buffer:
                    # Alternative: Bring order to cell output to minize the amount of items in this cell
                    # destination = self.cell.output_buffer

        else:
            # Order is in distribution cell

            if self.distribution_simple:
                destination = self.simple_distribution(next_processing_step)
            elif self.distribution_opt:
                destination = self.optimized_distribution(order, next_steps)
            elif self.distribution_smart:
                destination = self.smart_distribution(order, next_steps)

            if not destination:
                if self.cell.storage.free_slots() and order.position is not self.cell.storage:
                    destination = self.cell.storage

        if destination == order.position:
            return None
        return destination

    def simple_distribution(self, next_processing_step):
        """Item is in distribution cell: Get next position for the item. Always choose random between an useable child that has a free input slot.
                :param next_processing_step: The next processing steps that should be performed on this item
                :return destination: Calculated by simple distribution"""

        # Input of cell has free slots
        possibilities = [(cell, dict(cell.performable_tasks)) for cell in self.cell.childs if cell.input_buffer.free_slots()]
        # Next task can be processed by this cell/tree branch
        possibilities = [cell.input_buffer for (cell, tasks) in possibilities if tasks[next_processing_step] > 0]

        if len(possibilities) > 1:
            return random.choice(possibilities)
        elif possibilities:
            return possibilities[0]
        else:
            return None

    def optimized_distribution(self, item, next_steps):
        """Item is in distribution cell: Get next position for the item. Always prefer those child cell that have an free
        input slot and can perform the maximal amount of consecutive processing steps for this item. Not useful for
        full utilization and setups with high storage, input buffer capacities.
        :param item: (Order object) The item for which the destination should be calculated
        :param next_steps: list of all next processing steps that should be performed on this item
        :return destination: Calculated by optimized distribution"""

        destination = None
        possibilities = [(cell, cell.check_best_path(item, include_all=False), cell.performable_tasks) for cell in self.cell.childs]
        
        # Check all Child cells and sort by least amount of
        # manufacturing cells needed to completely process this order

        best_possibilities = sorted(
            [(cell, shortest_path, cell.input_buffer.free_slots()) for (cell, shortest_path, performable_tasks) in
             possibilities if shortest_path], key=lambda tup: tup[1])

        free_best_destinations = [cell.input_buffer for (cell, shortest_path, free_slots) in best_possibilities if
                                  free_slots]

        if free_best_destinations:
            destination = free_best_destinations[0]

        else:
            # Prefer the one that can perform the most continuous tasks and has a free Input Slot.
            result = [(cell, consecutive_performable_tasks(next_steps, performable_tasks)) for
                      (cell, shortest_path, performable_tasks) in possibilities]

            result = sorted([(cell, amount) for (cell, amount) in result if amount > 0], key=lambda tup: tup[1],
                            reverse=True)

            if result:
                for cell, amount in result:
                    if not cell.input_buffer.full:
                        best_cell = cell
                        destination = best_cell.input_buffer
                        break

        return destination
    
    def smart_distribution(self, item, next_steps):

        return None 
    

    def item_from_to(self, item, from_pos, to_pos):
        """TASK: Pick up an item and store it at another position within his cell. Performs all needed Subtasks

        :param item: (Order object) The item that should be moved
        :param from_pos: (Buffer or Machine object) Position where the item should be picked up
        :param to_pos: (Buffer or Machine object) Position where the item should be stored in the end
        """
        if from_pos == to_pos:
            raise Exception("The item is already at its target destination")

        # Move to position where the item is
        if self.position != from_pos:
            self.current_subtask = self.env.process(self.moving_proc(from_pos))
            yield self.current_subtask

        # Pick up item
        if not self.picked_up_item:
            self.current_subtask = self.env.process(self.pick_up(item))
            yield self.current_subtask

        # Move to target destination
        self.current_subtask = self.env.process(self.moving_proc(to_pos))
        yield self.current_subtask

        # Store item at destination
        self.current_subtask = self.env.process(self.store_item())
        yield self.current_subtask

        self.unlock_item()

        self.current_task = None

        # Inform agents in own cell and new cell if item was put in an interface
        self.cell.inform_agents()
        if item.current_cell is not self.cell and item.current_cell:
            item.current_cell.inform_agents()

    def announce_arrival(self, order, destination):
        """Announce the arrival of an order to the target destination. The announcement will contain the agent, item and time

        :param order: (Order object) Item to be announced to the destination
        :param destination: (Buffer or Machine object) Destination where the item will arrive"""
        # Calculate arrival time
        arr_time = self.env.now + self.time_for_distance(order.position) + self.time_for_distance(destination, start_position=order.position) + self.time_for_item_pick_up + self.time_for_item_store

        # Send announcement
        destination.expected_orders.append((order, arr_time, self))

        # Inform cell if destination is an interface buffer
        if isinstance(destination, InterfaceBuffer):
            if destination.upper_cell == self.cell:
                destination.lower_cell.inform_incoming_order(self, order, arr_time, destination)

            elif destination.upper_cell is not None:
                destination.upper_cell.inform_incoming_order(self, order, arr_time, destination)

    def moving_proc(self, destination):
        """SUBTASK: Agent is moving to its target position.
        :param destination: (Machine or Buffer object) The target position"""

        if isinstance(destination, Machine) and self.picked_up_item:
            if self.picked_up_item.next_task != destination.performable_task:
                raise Exception("Warning: An agent tried to move an item to an machine that can not perform the next processing step of the item!")

        if not self.moving and self.position != destination:
            self.start_moving(destination)
        else:
            return

        # Perform moving (Wait remaining moving time and change status afterwards)
        yield self.env.timeout(self.remaining_moving_time)

        self.end_moving()
        self.current_subtask = None

    def pick_up(self, item):
        """SUBTASK: Pick up item from position if no other item is picked up by this agent
        :param item: (Order object) The item to pick up"""

        # Check for errors
        if isinstance(self.position, Machine):
            if self.position.item_in_output != item:
                raise Exception("Agent can not pick up the next item, because it is currently not in the machine output!")
        else:
            if not item in self.position.items_in_storage:
                raise Exception("Agent can not pick up the next item, because it is not in the storage slots!")
            elif item.current_cell is not self.cell:
                raise Exception("Agent can not pick up the next item, because it is not in the same cell as the agent!")

        if self.picked_up_item is None:
            self.save_event("pick_up_start")

            # Perform picking up
            yield self.env.timeout(self.time_for_item_pick_up)

            # State changes after pick up
            self.end_pick_up(item)

        # Inform other agents within this cell
        self.cell.inform_agents()

        try:
            for child in self.cell.childs:
                child.inform_agents()
        except:
            pass

        self.current_subtask = None

    def store_item(self):
        """SUBTASK: Put down item at a machine or buffer and inform position."""
        if self.picked_up_item:
            item = self.picked_up_item
        else:
            raise Exception("Agent can not store an item because no item was picked up before!")

        full = False
        lock = False

        if isinstance(self.position, Buffer):
            if self.position.full:
                full = True
        else:
            if self.position.item_in_input or self.position.input_lock:
                full = True
            else:
                self.position.input_lock = True
                lock = True

        if full:
            self.current_waitingtask = self.env.process(self.wait_for_free_slot())
            yield self.current_waitingtask

            self.current_subtask = self.env.process(self.store_item())
            yield self.current_subtask
            return

        self.save_event("store_item_start")

        yield self.env.timeout(self.time_for_item_store)

        if lock:
            self.position.input_lock = False

        self.position.item_stored(item, self.cell)

        self.picked_up_item = None
        item.picked_up_by = None
        item.save_event("put_down")

        self.current_subtask = None
        self.save_event("store_item_end")

    def wait_for_free_slot(self):
        """SUBTASK: Endless loop. Wait for an item slot at current position to be free again.
        Interruption removes waiting agent from position. Loop can only be interrupted by simpy interruption"""
        try:
            self.position.waiting_agents.append(self)
            self.waiting = True
            self.save_event("wait_for_slot_start")

            while True:
                yield self.env.timeout(100000)

        except simpy.Interrupt as interruption:
            self.current_waitingtask = None
            self.waiting = False
            self.position.waiting_agents.remove(self)
            self.save_event("wait_for_slot_end")

    def unlock_item(self):
        """Release the locked item of the agent"""
        item = self.locked_item
        item.locked_by = None
        item.save_event("unlocked")
        self.locked_item = None

    def time_for_distance(self, destination, start_position=None):
        """Calculate the needed time for a given route

        :param destination: (Buffer or Machine object) Destination where the agend is heading to
        :param start_position: (Buffer or Machine object) Optional: Start position where the agent starts moving from. If None the agents position will be used
        :return time needed to move from start position to destination"""

        if not destination:
            raise Exception("Time for distance: Can not calculate the distance to destination None")

        def get_time(start_pos, end_pos):
            if start_pos == end_pos:
                return 0
            for start, end, length in self.cell.distances:
                if start == start_pos and end == end_pos:
                    return length / self.speed

        if not start_position:
            start_position = self.position

        if destination == start_position:
            return 0
        else:
            return get_time(start_position, destination)

    def state_change_in_cell(self):
        """The state of the agents cell has changed. If it has no current main process, start a new one"""
        if not self.main_proc.is_alive:
            self.main_proc = self.env.process(self.main_process())

    def start_moving(self, destination):
        """State changes for an agent that starts to move to an new destination.
        Sets the position of picked up items to None.
        :param destination: (Buffer or Machine object) The target position the agent is heading to"""

        self.moving = True
        self.moving_start_position = self.position
        self.moving_start_time = self.env.now
        self.next_position = destination
        self.moving_time = self.time_for_distance(destination)
        self.remaining_moving_time = self.moving_time
        self.moving_end_time = self.moving_start_time + self.moving_time

        if self.picked_up_item:
            self.picked_up_item.position = None
            self.picked_up_item.save_event("transportation_start")

        self.position = None

        self.save_event("moving_start", next_position=self.next_position, travel_time=self.remaining_moving_time)

    def end_moving(self):
        """State changes for an agent that reached its destination. Sets position for picked up items."""
        self.moving = False
        self.remaining_moving_time = 0
        self.moving_time = 0
        self.moving_end_time = None
        self.position = self.next_position
        self.next_position = None

        if self.picked_up_item:
            self.picked_up_item.position = self.position
            self.picked_up_item.save_event("transportation_end")

        self.save_event("moving_end")

    def end_pick_up(self, item):
        """State changes: Finished picking up an item
        :param item: (Order object) The picked up order"""
        self.picked_up_item = item

        item.picked_up_by = self
        item.position = None

        self.position.item_picked_up(item)

        item.save_event("picked_up")
        self.save_event("pick_up_end")

    def occupancy(self, attributes: list, requester=None):
        """State calculation for the agent. Gets agent attributes and picked up orders

        :param attributes: List of strings. Each element is an attribute that should be calculated and returned
        :param requester: (Agent object) Manufacturing agent that requests the state
        :return tuple of orders picked up and attributes of this agent. (list of dict, dict)"""

        if requester == self:
            pos_type = "Agent - Self"
        else:
            pos_type = "Agent"

        def agent_position():
            return self.position

        def moving():
            return int(self.moving)

        def remaining_moving_time():
            if self.moving:
                return self.moving_end_time - self.env.now
            else:
                return 0

        def next_position():
            if self.moving:
                return self.next_position
            else:
                return -1

        def has_task():
            return int(self.has_task)

        def locked_item():
            if self.locked_item:
                return self.locked_item
            else:
                return -1

        attr = {}
        for attribute in attributes:
            attr[attribute] = locals()[attribute]()

        if self.picked_up_item:
            return [{"order": self.picked_up_item, "pos": self, "pos_type": pos_type}], attr
        else:
            return [{"order": None, "pos": self, "pos_type": pos_type}], attr

    def save_event(self, event_type: str, next_position=None, travel_time=None):
        """Save an event to the event log database. Includes the current state of the object.

        :param event_type: (str) The title of the triggered event
        :param next_position: (Buffer object or Machine object) Only when a new moving process is started: The destination where the agent is going
        :param travel_time: (float) Only when a new moving process is started: The time needed to get to the next position"""

        if self.simulation_environment.train_model:
            return

        db = self.simulation_environment.db_con
        cursor = self.simulation_environment.db_cu

        time = self.env.now

        if next_position:
            nxt_pos = id(next_position)
        else:
            nxt_pos = None

        if self.position:
            pos = id(self.position)
        else:
            pos = None

        if self.picked_up_item:
            pui = id(self.picked_up_item)
        else:
            pui = None

        if self.locked_item:
            locki = id(self.locked_item)
        else:
            locki = None

        cursor.execute("INSERT INTO agent_events VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (id(self), time, event_type, nxt_pos, travel_time, self.moving, self.waiting, self.has_task, pos,
                        pui, locki))
        db.commit()

    def initial_event(self):
        """Add initial events to event log database. Necessary to calculate measures and results"""
        self.save_event("Initial")
        yield self.env.timeout(0)

    def end_event(self):
        """Add end events to event log database. Necessary to calculate measures and results"""
        self.save_event("End_of_Time")

    def state_to_numeric(self, order_state):
        """Util: Converts an categorical state into an numerical state. Fill all Nan values.
        :param order_state: Pandas Dataframe containing the categorical state
        :return numerical_order_state: Converted State"""
        
        # Add index column
        order_state.loc[:, "slot_id"] = order_state.index
        slot_ids = order_state.pop("slot_id")
        order_state.insert(0, "slot_id", slot_ids)
        
        a = order_state.loc[:, "pos_type"]
        
        # Get ids for all positions within the cell
        pos_in_cell = order_state["pos"].unique()
        pos_ids = np.arange(1, len(pos_in_cell) + 1)
        pos_ids = dict(zip(pos_in_cell, pos_ids))

        # Get ids for all position types
        pos_type_ids = dict_pos_types

        # Get all orders currently within this cell
        orders_in_cell = order_state[order_state["order"].notnull()]["order"].to_dict()
        orders_in_cell = {orders_in_cell[key]: key for key in orders_in_cell}

        # Map categorical values to ids
        cols = ["pos", "agent_position", "next_position", "_destination"]
        cols = [column for column in cols if column in order_state.columns.values.tolist()]
        order_state[cols] = order_state[cols].replace(pos_ids)
        
        cols = ["pos_type"]
        order_state[cols] = order_state[cols].replace(pos_type_ids)
        
        if "locked_item" in order_state.columns.values.tolist():
            order_state["locked_item"].fillna(-2)
            cols = ["locked_item"]
            order_state[cols] = order_state[cols].replace(orders_in_cell)
        
        order_state = order_state.fillna(0)
        now = time.time()
        order_state.loc[order_state["order"] != 0, "order"] = 1
        time_tracker.time_prob_2 += time.time() - now
        return order_state


    def save_action(self, cell_state, action, next_order):
        agent_name = str(self)
        agent_name = agent_name[-14:-1]
        try:
            priority = cell_state.loc[action, "priority"].values[0]
        except:
            priority = None

        dest =  None
        try:
            for i in range(len(cell_state)):
                if (cell_state.loc[i, "pos"]) == (cell_state.loc[action, "_destination"].values[0]): 
                    dest_dest = cell_state.loc[i, "pos_type"]
                    dest_dest_1 = cell_state.loc[i, "pos"]
        except:
            dest_dest = None
            dest_dest_1 = None

        try:
            id_dest_dest = dest_dest_1.lower_cell.id 
        except:
            id_dest_dest = 7

        try:
            for i in range(len(cell_state)):
                if (cell_state.loc[i, "pos"]) == (cell_state.loc[action, "pos"].values[0]): 
                    dest = cell_state.loc[i, "pos_type"]
                    dest_1 = cell_state.loc[i, "pos"]
        except:
            dest = None
            dest_1 = None

        try:
            id_dest = dest_1.lower_cell.id 
        except:
            id_dest = 7
        
        for i in range(len(cell_state)):
            if (cell_state.loc[i, "pos"]) == self.position: 
                start_pos = cell_state.loc[i, "pos_type"]
                start_pos_1 = cell_state.loc[i, "pos"]
        try:
            id_start = start_pos_1.lower_cell.id 
        except:
            id_start = 7
        
        try:
            next_order_start= round(next_order.start,2)
        except:
            next_order_start = None
        
        with open('../result/rewards/action_operations_' + self.timestamp + '_' + "cell.id-" + str(self.cell.id) + '_agent-' + agent_name + '_level-' + str(self.cell.level) + '_rule-' + str(self.ruleset.id) +  '.csv', 'a+', newline='', encoding='utf-8') as file:
            writer = csv.writer(file)
            # writer.writerow(list([self.cell.id, self.ruleset.id, agent_name, priority, next_order, next_order_start, round(self.env.now,2), id_start, str(start_pos),id_dest, str(dest), action]))
            writer.writerow(list([self.cell.id, self.ruleset.id, agent_name, priority, next_order, next_order_start, round(self.env.now,2), id_start, id_dest, id_dest_dest, str(start_pos) , str(dest), str(dest_dest),action]))
        

        if dest == "Input":
            id_dest = 6
        elif dest == "Output":
            id_dest = 7
        elif dest == "Storage":
            id_dest = 8
        if dest_dest == "Input":
            id_dest_dest = 6
        elif dest_dest == "Output":
            id_dest_dest = 7
        elif dest_dest == "Storage":
            id_dest_dest = 8

        if self.init_pos:
            with open('../result/operations/action_operations_FAZI_' + self.timestamp + '_' + "cell.id-" + str(self.cell.id) + '_agent-' + agent_name + '_level-' + str(self.cell.level) + '_rule-' + str(self.ruleset.id) +  '.csv', 'a+', newline='', encoding='utf-8') as file:
                writer = csv.writer(file)
                writer.writerow(list([id_start, round(self.env.now,2)]))
            self.init_pos = False
        if dest != None:
            with open('../result/operations/action_operations_FAZI_' + self.timestamp + '_' + "cell.id-" + str(self.cell.id) + '_agent-' + agent_name + '_level-' + str(self.cell.level) + '_rule-' + str(self.ruleset.id) +  '.csv', 'a+', newline='', encoding='utf-8') as file:
                writer = csv.writer(file)
                writer.writerow(list([id_dest, str(dest), round(self.env.now,2)]))
        if dest_dest != None:
            with open('../result/operations/action_operations_FAZI_' + self.timestamp + '_' + "cell.id-" + str(self.cell.id) + '_agent-' + agent_name + '_level-' + str(self.cell.level) + '_rule-' + str(self.ruleset.id) +  '.csv', 'a+', newline='', encoding='utf-8') as file:
                writer = csv.writer(file)
                writer.writerow(list([id_dest_dest, str(dest_dest), round(self.env.now,2)]))

