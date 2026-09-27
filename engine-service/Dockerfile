# Award Engine service. The award-intelligence checkout (with its node_modules
# and .git, for the pin check) is mounted at runtime, not baked in:
#   docker run -p 8790:8790 -v /path/to/award-intelligence:/opt/award-intelligence:ro orl-engine
FROM node:22-alpine
WORKDIR /app
COPY package.json award-intelligence.pin ./
COPY src ./src
ENV AWARD_INTELLIGENCE_PATH=/opt/award-intelligence \
    ENGINE_HOST=0.0.0.0 \
    ENGINE_PORT=8790
EXPOSE 8790
USER node
CMD ["node", "src/server.js"]
