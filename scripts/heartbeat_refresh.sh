#!/bin/bash
NOW=$(date +%s)

# Strategy heartbeat
docker exec quantmind-redis redis-cli SET 'quantmind:strategy:status:default:10000002:default' "{\"last_seen\":$NOW,\"status\":\"running\",\"metrics\":{\"positions\":8},\"strategy_nav\":1.0,\"pod_name\":\"kaitosim-signal-runner\"}" EX 300 >/dev/null 2>&1

docker exec quantmind-redis redis-cli SET 'quantmind:strategy:status:default:10000002:signal_momentum_v1' "{\"strategy_id\":\"signal_momentum_v1\",\"status\":\"running\",\"metrics\":{},\"strategy_nav\":1.0,\"pod_name\":\"kaitosim-signal-runner\"}" EX 300 >/dev/null 2>&1

# Real trading status
docker exec quantmind-redis redis-cli SET 'real_trading:status:default:10000002' "{\"status\":\"running\",\"mode\":\"SIMULATION\",\"strategy\":{\"id\":\"44\",\"name\":\"信號動量策略\"},\"started_at\":$NOW}" EX 300 >/dev/null 2>&1

# Deployment status
docker exec quantmind-redis redis-cli SET 'quantmind:deployment:default:10000002' "{\"mode\":\"simulation\",\"channel\":\"kaitosim\",\"status\":\"online\"}" EX 300 >/dev/null 2>&1
