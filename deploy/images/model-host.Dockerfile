# A vLLM host that can serve the Fabric decode kernel instead of vLLM's own.
#
# One image serves both sides of a comparison. The substitution is inert unless
# FABRIC_KERNEL is set, so two hosts differing only in that variable differ in the kernel
# and nothing else; two images would leave every other difference between them as a
# candidate explanation for a difference in speed.
#
# The entrypoint is inherited deliberately. The operator passes the server's own flags, and
# an image that wrapped the entrypoint would have to keep pace with them.
FROM vllm/vllm-openai:v0.26.0

COPY runtime/ /opt/fabric/runtime/
COPY serving/fabric_serving/ /opt/fabric/serving/fabric_serving/
# Imported automatically by every interpreter that starts, which is the only placement
# that reaches the process running the model: vLLM runs its engine in a child process, so
# a substitution installed only by the parent would not be present where decoding happens.
COPY serving/sitecustomize.py /opt/fabric/serving/sitecustomize.py

ENV PYTHONPATH=/opt/fabric/serving:/opt/fabric/runtime
