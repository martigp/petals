from transformers import AutoTokenizer
import time
from petals import AutoDistributedModelForCausalLM
import itertools
from hivemind import get_logger
INITIAL_PEERS = [
        "/ip4/172.28.5.2/tcp/9000/p2p/QmaFMcNeEjz7U8AeqF6euSFZz4c1LAyuJhpYAf7DtPJkHs",
]
model_name = "bigscience/bloom-560m"
logger = get_logger(__name__)

time.sleep(75)
# Connect to a distributed network hosting model layers
tokenizer = AutoTokenizer.from_pretrained(model_name)
model = AutoDistributedModelForCausalLM.from_pretrained(model_name, initial_peers = INITIAL_PEERS)

# Run the model as if it were on your computer
for i in itertools.count():
        logger.info(f"Iteration Step {i} #############################")
        inputs = tokenizer("A cat sat", return_tensors="pt")["input_ids"]
        outputs = model.generate(inputs, max_new_tokens=5)


