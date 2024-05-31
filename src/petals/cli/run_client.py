from transformers import AutoTokenizer
from petals import AutoDistributedModelForCausalLM
INITIAL_PEERS = [
        "/ip4/172.28.5.2/tcp/9000/p2p/QmaFMcNeEjz7U8AeqF6euSFZz4c1LAyuJhpYAf7DtPJkHs",
]
model_name = "bigscience/bloom-560m"

# Connect to a distributed network hosting model layers
tokenizer = AutoTokenizer.from_pretrained(model_name)
model = AutoDistributedModelForCausalLM.from_pretrained(model_name, initial_peers = INITIAL_PEERS)

# Run the model as if it were on your computer
inputs = tokenizer("A cat sat", return_tensors="pt")["input_ids"]
outputs = model.generate(inputs, max_new_tokens=5)
print(tokenizer.decode(outputs[0]))  # A cat sat on a mat...

