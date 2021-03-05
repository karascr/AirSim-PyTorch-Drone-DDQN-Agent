import math
import random
from collections import deque
import airsim
import os
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from PIL import Image
from setuptools import glob
from env import DroneEnv
from torch.utils.tensorboard import SummaryWriter
import time

writer = SummaryWriter()

torch.manual_seed(0)
random.seed(0)
np.random.seed(0)

class DQN(nn.Module):
    def __init__(self, in_channels=1, num_actions=4):
        super(DQN, self).__init__()
        self.conv1 = nn.Conv2d(in_channels, 84, kernel_size=4, stride=4)
        self.conv2 = nn.Conv2d(84, 42, kernel_size=4, stride=2)
        self.conv3 = nn.Conv2d(42, 21, kernel_size=2, stride=2)
        self.fc4 = nn.Linear(21*4*4, 168)
        self.fc5 = nn.Linear(168, num_actions)

    def forward(self, x):
        x = F.relu(self.conv1(x))
        x = F.relu(self.conv2(x))
        x = F.relu(self.conv3(x))
        x = x.view(x.size(0), -1)
        x = F.relu(self.fc4(x))
        return self.fc5(x)

class DDQN_Agent:
    def __init__(self, useGPU=False, useDepth=False):
        self.useGPU = useGPU
        self.useDepth = useDepth
        self.eps_start = 0.9
        self.eps_end = 0.05
        self.eps_decay = 30000
        self.gamma = 0.8
        self.learning_rate = 0.001
        self.batch_size = 256
        self.max_episodes = 10000
        self.save_interval = 10
        self.test_interval = 2
        self.episode = -1
        self.steps_done = 0
        self.network_update_interval = 10

        if self.useGPU and torch.cuda.is_available():
            self.device = torch.device('cuda:0')
        else:
            self.device = torch.device('cpu')

        self.policy = DQN()
        self.target = DQN()
        self.test_network = DQN()
        self.target.eval()
        self.test_network.eval()
        self.updateNetworks()

        self.env = DroneEnv(useGPU, useDepth)
        self.memory = deque(maxlen=10000)
        self.optimizer = optim.Adam(self.policy.parameters(), self.learning_rate)

        print('Using device:', self.device)
        if torch.cuda.is_available():
            print(torch.cuda.get_device_name(0))

        # LOGGING
        cwd = os.getcwd()
        self.save_dir = os.path.join(cwd, "saved models")
        if not os.path.exists(self.save_dir):
            os.mkdir("saved models")

        if self.useGPU:
            self.policy = self.policy.to(self.device)  # to use GPU
            self.target = self.target.to(self.device)  # to use GPU
            self.test_network = self.test_network.to(self.device)  # to use GPU

        # model backup
        files = glob.glob(self.save_dir + '\\*.pt')
        if len(files) > 0:
            files.sort(key=os.path.getmtime)
            file = files[-1]
            checkpoint = torch.load(file)
            self.policy.load_state_dict(checkpoint['state_dict'])
            self.episode = checkpoint['episode']
            self.steps_done = checkpoint['steps_done']
            self.optimizer.load_state_dict(checkpoint['optimizer'])
            self.updateNetworks()
            print("Saved parameters loaded"
                  "\nModel: ", file,
                  "\nSteps done: ", self.steps_done,
                  "\nEpisode: ", self.episode)

        else:
            if os.path.exists("log.txt"):
                open('log.txt', 'w').close()
            if os.path.exists("last_episode.txt"):
                open('last_episode.txt', 'w').close()
            if os.path.exists("last_episode.txt"):
                open('saved_model_params.txt', 'w').close()

        obs = self.env.reset()
        tensor = self.transformToTensor(obs)
        writer.add_graph(self.policy, tensor)

    def updateNetworks(self):
        self.target.load_state_dict(self.policy.state_dict())

    def transformToTensor(self, img):
        if self.useGPU:
            tensor = torch.cuda.FloatTensor(img)
        else:
            tensor = torch.Tensor(img)
        tensor = tensor.unsqueeze(0)
        tensor = tensor.unsqueeze(0)
        tensor = tensor.float()
        return tensor

    def convert_size(self, size_bytes):
        if size_bytes == 0:
            return "0B"
        size_name = ("B", "KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB")
        i = int(math.floor(math.log(size_bytes, 1024)))
        p = math.pow(1024, i)
        s = round(size_bytes / p, 2)
        return "%s %s" % (s, size_name[i])

    def act(self, state):
        self.eps_threshold = self.eps_end + (self.eps_start - self.eps_end) * math.exp(
            -1.0 * self.steps_done / self.eps_decay
        )
        self.steps_done += 1
        if random.random() > self.eps_threshold:
            #print("greedy")
            if self.useGPU:
                action = np.argmax(self.policy(state).cpu().data.squeeze().numpy())
                return int(action)
            else:
                data = self.policy(state).data
                action = np.argmax(data.squeeze().numpy())
                return int(action)

        else:
            action = random.randrange(0, 4)
            return int(action)

    def memorize(self, state, action, reward, next_state):
        self.memory.append(
            (
                state,
                action,
                torch.cuda.FloatTensor([reward]) if self.useGPU else torch.FloatTensor([reward]),
                self.transformToTensor(next_state),
            )
        )

    def learn(self):
        if len(self.memory) < self.batch_size:
            return

        batch = random.sample(self.memory, self.batch_size)
        states, actions, rewards, next_states = zip(*batch)

        states = torch.cat(states)
        actions = np.asarray(actions)
        rewards = torch.cat(rewards)
        next_states = torch.cat(next_states)

        if self.useGPU:
            current_q = torch.cuda.FloatTensor(self.policy(states)[[range(0, self.batch_size)], [actions]])
            next_q_values = self.target(next_states).cpu().detach().numpy()
            max_next_q = torch.cuda.FloatTensor(next_q_values[[range(0, self.batch_size)], [actions]])
            expected_q = rewards.to(self.device) + (self.gamma * max_next_q).to(self.device)
        else:
            current_q = self.policy(states)[[range(0, self.batch_size)], [actions]]
            next_q_values = self.target(next_states).detach().numpy()
            max_next_q = next_q_values[[range(0, self.batch_size)], [actions]]
            expected_q = rewards + (self.gamma * max_next_q)

        loss = F.mse_loss(current_q.squeeze(), expected_q.squeeze())
        print("loss: ", loss, "---", loss.data)
        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()

    def train(self):
        score_history = []
        reward_history = []
        if self.episode == -1:
            self.episode = 1

        for e in range(1, self.max_episodes + 1):
            start = time.time()
            state = self.env.reset()
            steps = 0
            score = 0
            while True:
                state = self.transformToTensor(state)

                action = self.act(state)
                next_state, reward, done = self.env.step(action)

                if steps == 34:
                    done = 1
                    print("Max step size reached: ", steps)

                self.memorize(state, action, reward, next_state)
                self.learn()

                state = next_state
                steps += 1
                score += reward
                if done:
                    print("----------------------------------------------------------------------------------------")
                    print("episode:{0}, reward: {1}, mean reward: {2}, score: {3}, epsilon: {4}, total steps: {5}".format(self.episode, reward, round(score/steps, 2), score, self.eps_threshold, self.steps_done))
                    score_history.append(score)
                    reward_history.append(reward)
                    with open('log.txt', 'a') as file:
                        file.write("episode:{0}, reward: {1}, mean reward: {2}, score: {3}, epsilon: {4}, total steps: {5}\n".format(self.episode, reward, round(score/steps, 2), score, self.eps_threshold, self.steps_done))

                    if self.useGPU:
                        print('Total Memory:', self.convert_size(torch.cuda.get_device_properties(0).total_memory))
                        print('Allocated Memory:', self.convert_size(torch.cuda.memory_allocated(0)))
                        print('Cached Memory:', self.convert_size(torch.cuda.memory_reserved(0)))
                        print('Free Memory:', self.convert_size(torch.cuda.get_device_properties(0).total_memory - (torch.cuda.max_memory_allocated() + torch.cuda.max_memory_reserved())))

                        # tensorboard --logdir=runs
                        memory_usage_allocated = np.float64(round(torch.cuda.memory_allocated(0) / 1024 ** 3, 1))
                        memory_usage_cached = np.float64(round(torch.cuda.memory_reserved(0) / 1024 ** 3, 1))

                        writer.add_scalar("memory_usage_allocated", memory_usage_allocated, self.episode)
                        writer.add_scalar("memory_usage_cached", memory_usage_cached, self.episode)

                    writer.add_scalar('epsilon_value', self.eps_threshold, self.episode)
                    writer.add_scalar('score_history', score, self.episode)
                    writer.add_scalar('reward_history', reward, self.episode)
                    writer.add_scalar('Total steps', self.steps_done, self.episode)
                    writer.add_scalars('General Look', {'epsilon_value': self.eps_threshold,
                                                    'score_history': score,
                                                    'reward_history': reward}, self.episode)

                    # save checkpoint
                    if self.episode % self.save_interval == 0:
                        checkpoint = {
                            'episode': self.episode,
                            'steps_done': self.steps_done,
                            'state_dict': self.policy.state_dict(),
                            'optimizer': self.optimizer.state_dict()
                        }
                        torch.save(checkpoint, self.save_dir + '//EPISODE{}.pt'.format(self.episode))

                    if self.episode % self.network_update_interval == 0:
                        self.updateNetworks()

                    self.episode += 1
                    end = time.time()
                    stopWatch = end - start
                    print("Episode is done, episode time: ", stopWatch)

                    if self.episode % self.test_interval == 0:
                        self.test()

                    break
        writer.close()

    def test(self):
        self.test_network.load_state_dict(self.target.state_dict())

        start = time.time()
        steps = 0
        score = 0
        state = self.env.reset()

        while True:
            state = self.transformToTensor(state)

            action = int(np.argmax(self.test_network(state).cpu().data.squeeze().numpy()))
            next_state, reward, done = self.env.step(action)

            if steps > 34:
                done = 1

            state = next_state
            steps += 1
            score += reward

            if done:
                print("----------------------------------------------------------------------------------------")
                print("TEST, reward: {}, score: {}, total steps: {}".format(
                        reward, score, self.steps_done))

                with open('tests.txt', 'a') as file:
                    file.write("TEST, reward: {}, score: {}, total steps: {}".format(
                        reward, score, self.steps_done))

                writer.add_scalars('Test', {'score': score, 'reward': reward}, self.episode)

                end = time.time()
                stopWatch = end - start
                print("Test is done, test time: ", stopWatch)

                break