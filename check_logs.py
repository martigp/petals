import argparse
import datetime

parser = argparse.ArgumentParser(description='Check logs for errors')
parser.add_argument('logfile', type=str, help='The log file to be checked')
args = parser.parse_args()

timestamps = []
with open(args.logfile) as f:
    for line in f:
        if 'Iteration Step' in line:
            s = line.split()[4]
            element = datetime.datetime.strptime(s, '%H:%M:%S.%f') 
            timestamps.append(datetime.datetime.timestamp(element))

print(f"Total Iterations: {len(timestamps)-1}, Time taken: {timestamps[-1] - timestamps[0]} seconds", end=" ")
print(f"Average time per iteration: {(timestamps[-1] - timestamps[0])/(len(timestamps)-1)} seconds")