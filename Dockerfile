FROM nvcr.io/nvidia/cuda:11.0.3-cudnn8-devel-ubuntu20.04 as base
LABEL maintainer="bigscience-workshop"
LABEL repository="petals"

WORKDIR /home
# Set en_US.UTF-8 locale by default
RUN echo "LC_ALL=en_US.UTF-8" >> /etc/environment

# Install packages
RUN apt-get update && apt-get install -y --no-install-recommends \
  build-essential \
  wget \
  git \
  && apt-get clean autoclean && rm -rf /var/lib/apt/lists/{apt,dpkg,cache,log} /tmp/* /var/tmp/*

RUN wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -O install_miniconda.sh && \
  bash install_miniconda.sh -b -p /opt/conda && rm install_miniconda.sh
ENV PATH="/opt/conda/bin:${PATH}"

RUN conda install python~=3.10.12 pip && \
    pip install --no-cache-dir "torch>=1.12" && \
    conda clean --all && rm -rf ~/.cache/pip

VOLUME /cache
ENV PETALS_CACHE=/cache

COPY base_requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

COPY . petals/
RUN pip install --no-cache-dir -e ./petals

WORKDIR /home/petals
CMD [ "bash" ]

ENV TRUSTED_PEERS='/ip4/172.28.5.2/tcp/9000/p2p/QmeMMaJCua1NMtiCzYVbYkvaFoVUaRsuo8jX18SfK6CdyB'

FROM base as backbone
# Backbone P2P id is p2p/QmaFMcNeEjz7U8AeqF6euSFZz4c1LAyuJhpYAf7DtPJkHs
CMD [ "python", "src/petals/cli/run_dht.py", "--identity_path", "backbone.id", "--host_maddrs", "/ip4/0.0.0.0/tcp/9000" ]

FROM base as server
CMD [ "python", "src/petals/cli/run_server.py", "bigscience/bloom-560m", "--num_blocks", "12", "--initial_peers", "/ip4/172.28.5.2/tcp/9000/p2p/QmaFMcNeEjz7U8AeqF6euSFZz4c1LAyuJhpYAf7DtPJkHs" ]

FROM base as trusted_server
CMD [ "python", "src/petals/cli/run_server.py", "bigscience/bloom-560m", "--identity_path", "trusted_server.id" , "--host_maddrs", "/ip4/0.0.0.0/tcp/9001", "--num_blocks", "12", "--initial_peers", "/ip4/172.28.5.2/tcp/9000/p2p/QmaFMcNeEjz7U8AeqF6euSFZz4c1LAyuJhpYAf7DtPJkHs" ]

FROM base as client
CMD [ "python", "src/petals/cli/run_client.py"]

FROM base as malicious_server
CMD [ "python", "src/petals/cli/run_server.py", "bigscience/bloom-560m", "--num_blocks", "12", "--initial_peers", "/ip4/172.28.5.2/tcp/9000/p2p/QmaFMcNeEjz7U8AeqF6euSFZz4c1LAyuJhpYAf7DtPJkHs", "--malicious", "True" ]
