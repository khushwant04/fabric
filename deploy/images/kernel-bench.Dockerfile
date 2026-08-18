# Runs the kernel comparison inside the same environment that serves the model.
#
# The comparison has to happen against the vLLM the host actually runs, on the GPU the
# host actually uses. Measuring on a development GPU answers a question about a different
# compute capability, and measuring against a different vLLM answers a question about a
# kernel the host would never call.
FROM vllm/vllm-openai:v0.26.0

COPY runtime/ /opt/fabric/runtime/
COPY serving/fabric_serving/ /opt/fabric/serving/fabric_serving/

# Both trees are importable, so the comparison can reach the Fabric kernel and vLLM's own
# op without depending on where it is run from.
ENV PYTHONPATH=/opt/fabric/serving:/opt/fabric/runtime
WORKDIR /opt/fabric/serving

ENTRYPOINT ["python3", "-m", "fabric_serving.compare_packed"]
