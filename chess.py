import asyncio
import json
import logging
import os
import signal
import subprocess
import time
import ipaddress
import re
from concurrent.futures import ProcessPoolExecutor

# Load configurations from config.json
with open("config.json") as f:
    config = json.load(f)

HOST = config["host"]
BASE_LOG_DIR = config["base_log_dir"]
ENGINES = config["engines"]
CUSTOM_VARIABLES = config["custom_variables"]
MAX_CONNECTIONS = config["max_connections"]
# Dictionary to store connection attempts for each IP address
connection_attempts = {}

# Configure logging
if config["enable_server_log"]:
    server_log_file = os.path.join(BASE_LOG_DIR, "server.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(server_log_file),
            logging.StreamHandler()
        ]
    )
else:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler()
        ]
    )
sem = asyncio.Semaphore(MAX_CONNECTIONS)


async def watchdog_timer(interval):
    while True:
        await asyncio.sleep(interval)
        logging.info("Watchdog timer: Server is responsive")


async def heartbeat(writer, interval):
    while True:
        try:
            writer.write(b"\nping\n")
            await writer.drain()
            await asyncio.sleep(interval)
        except Exception as e:
            logging.error(f"Heartbeat error: {e}")
            break


def generate_subnets_to_avoid(ip_addresses_to_avoid, subnets_to_avoid):
    # Ensure all inputs are converted to network objects
    ip_addresses_to_avoid = [ipaddress.ip_network(ip + '/32') for ip in ip_addresses_to_avoid]
    subnets_to_avoid = [ipaddress.ip_network(subnet) for subnet in subnets_to_avoid]

    # Define public IP address ranges
    public_ranges = [
        ipaddress.ip_network('1.0.0.0/8'),
        ipaddress.ip_network('2.0.0.0/7'),
        ipaddress.ip_network('4.0.0.0/6'),
        ipaddress.ip_network('8.0.0.0/7'),
        ipaddress.ip_network('11.0.0.0/8'),
        ipaddress.ip_network('12.0.0.0/6'),
        ipaddress.ip_network('16.0.0.0/4'),
        ipaddress.ip_network('32.0.0.0/3'),
        ipaddress.ip_network('64.0.0.0/2'),
        ipaddress.ip_network('128.0.0.0/2'),
        ipaddress.ip_network('192.0.0.0/9'),
        ipaddress.ip_network('208.0.0.0/4'),
        ipaddress.ip_network('224.0.0.0/3'),
    ]

    # Combine addresses and subnets to avoid into a single list
    addresses_to_exclude = ip_addresses_to_avoid + subnets_to_avoid

    subnets_to_use = []
    for public_range in public_ranges:
        current_ranges = [public_range]
        for address_to_exclude in addresses_to_exclude:
            new_ranges = []
            for current_range in current_ranges:
                # Use address_exclude to subtract address_to_exclude from current_range
                try:
                    excluded = current_range.address_exclude(address_to_exclude)
                    new_ranges.extend(excluded)
                except ValueError:
                    # If address_to_exclude is not a subnet of current_range, keep current_range
                    new_ranges.append(current_range)
            current_ranges = new_ranges
        subnets_to_use.extend(current_ranges)

    return [str(subnet) for subnet in subnets_to_use
]


async def async_generate_subnets_to_avoid(ip_addresses_to_avoid, subnets_to_avoid):
    loop = asyncio.get_running_loop()
    # Run the CPU-bound function in a separate process to avoid blocking the event loop
    with ProcessPoolExecutor() as pool:
        result = await loop.run_in_executor(pool, generate_subnets_to_avoid, ip_addresses_to_avoid, subnets_to_avoid)
    return result
    
   
    
async def block_ip_address(ip_address, ports):
    logging.debug(f"Entering block_ip_address for IP {ip_address}")
    if not all(ipaddress.ip_address(ip_address).is_global for ip_address in [ip_address]):
        logging.warning(f"Skipping blocking of non-global IP address: {ip_address}")
        return

    # Check if the IP address is already blocked
    check_cmd = ["netsh", "advfirewall", "firewall", "show", "rule", "name=Chess-Block-IPs"]
    process_check = subprocess.run(check_cmd, capture_output=True, text=True)

    if process_check.returncode == 0:
        # If the Chess-Block-IPs rule exists, get the existing blocked IPs
        existing_ips = re.findall(r"RemoteIP:\s*(.*)", process_check.stdout)
        if existing_ips:
            existing_ips = existing_ips[0].split(",")
            if ip_address in existing_ips:
                logging.info(f"IP {ip_address} is already blocked by the Chess-Block-IPs rule")
                return
        else:
            existing_ips = []

        # Add the new IP address to the existing blocked IPs
        updated_ips = ",".join(existing_ips + [ip_address])
        set_cmd = [
            "netsh", "advfirewall", "firewall", "set", "rule", "name=Chess-Block-IPs",
            "new", "remoteip=" + updated_ips
        ]
        process_set = subprocess.run(set_cmd, check=False, capture_output=True, text=True)
        if process_set.returncode != 0:
            logging.error(f"Failed to update the Chess-Block-IPs rule with IP {ip_address}: {process_set.stderr}")
        else:
            logging.info(f"Added IP {ip_address} to the existing Chess-Block-IPs rule")
    else:
        # If the Chess-Block-IPs rule doesn't exist, create a new rule
        block_cmd = [
            "netsh", "advfirewall", "firewall", "add", "rule", "name=Chess-Block-IPs",
            "dir=in", "action=block", "protocol=TCP", "localport=" + ports,
            "remoteip=" + ip_address, "enable=yes"
        ]
        process_block = subprocess.run(block_cmd, check=False, capture_output=True, text=True)
        if process_block.returncode != 0:
            logging.error(f"Failed to create block rule for IP {ip_address}: {process_block.stderr}")
        else:
            logging.info(f"Created new Chess-Block-IPs rule and blocked IP {ip_address}")
    logging.debug(f"Exiting block_ip_address for IP {ip_address}")
        

def check_connection_attempts(client_ip):
    if client_ip in config["trusted_sources"]:
        return

    if any(ipaddress.ip_address(client_ip) in ipaddress.ip_network(subnet) for subnet in config["trusted_subnets"]):
        return

    current_time = time.time()

    # Remove expired connection attempts
    global connection_attempts
    connection_attempts = {ip: attempts for ip, attempts in connection_attempts.items()
                            if current_time - attempts[-1] <= config["connection_attempt_period"]}

    if client_ip not in connection_attempts:
        connection_attempts[client_ip] = []

    connection_attempts[client_ip].append(current_time)

    # Log untrusted connection attempt
    if config["Log_untrusted_connection_attempts"]:
        log_message = f"Untrusted connection attempt from {client_ip}. Attempt count: {len(connection_attempts[client_ip])}"
        logging.warning(log_message)
        with open(os.path.join(BASE_LOG_DIR, "untrusted_connection_attempts.log"), "a") as f:
            f.write(log_message + "\n")

    if len(connection_attempts[client_ip]) > config["max_connection_attempts"]:
        if config["enable_firewall_ip_blocking"]:
            logging.warning(f"Blocking IP {client_ip} due to excessive connection attempts")
            ports = ",".join(str(engine["port"]) for engine in ENGINES.values())
            asyncio.create_task(block_ip_address(client_ip, ports))

        # Log IP blocking event
        if config["Log_untrusted_connection_attempts"]:
            log_message = f"IP {client_ip} blocked due to excessive connection attempts. Attempt count: {len(connection_attempts[client_ip])}"
            logging.warning(log_message)
            with open(os.path.join(BASE_LOG_DIR, "untrusted_connection_attempts.log"), "a") as f:
                f.write(log_message + "\n")

        # Remove the blocked IP from the connection_attempts dictionary
        connection_attempts.pop(client_ip, None)
        
        

async def configure_firewall(config):
    if not config.get("enable_firewall_rules", False):
        logging.info("Firewall rules configuration is disabled. Skipping.")
        return

    ENGINES = config["engines"]
    ports = ",".join(str(engine["port"]) for engine in ENGINES.values())
    ip_addresses_to_avoid = config["trusted_sources"]
    subnets_to_avoid = config["trusted_subnets"]

    if config.get("enable_firewall_subnet_blocking", False):
        # Await the completion of subnet generation
        subnets_to_block = await async_generate_subnets_to_avoid(ip_addresses_to_avoid, subnets_to_avoid)

        delete_cmd = ["netsh", "advfirewall", "firewall", "delete", "rule", "name=Chess-Block-Other"]
        process_delete = subprocess.run(delete_cmd, check=False, capture_output=True, text=True)

        if process_delete.returncode != 0:
            stderr_output = process_delete.stderr
            if "No rules match the specified criteria" in stderr_output:
                logging.info("No existing Chess-Block-Other rules found. Proceeding.")
            else:
                logging.error(f"Failed to delete rule: {stderr_output}")
        else:
            logging.info("Existing Chess-Block-Other rules removed from Windows Firewall.")

        # Combine the subnets into a comma-separated string
        subnets_combined = ",".join(subnets_to_block)

        # Create a single block rule for all subnets
        block_cmd = [
            "netsh", "advfirewall", "firewall", "add", "rule", "name=Chess-Block-Other",
            "dir=in", "action=block", "protocol=TCP", "localport=" + ports,
            "remoteip=" + subnets_combined, "enable=yes"
        ]
        process_block = subprocess.run(block_cmd, check=False, capture_output=True, text=True)
        if process_block.returncode != 0:
            logging.error(f"Failed to add block rule: {process_block.stderr}")
        else:
            logging.info(f"Blocked inbound traffic for subnets {subnets_combined} on ports {ports}.")
            
        
        
async def engine_communication(engine_process, writer, log_file):
    while True:
        try:
            data = await asyncio.wait_for(engine_process.stdout.readline(), timeout=60)
            if not data:
                break

            log_message = f"Engine: {data.decode().strip()}\n"
            logging.info(log_message)

            writer.write(data)
            await writer.drain()

            with open(log_file, "a") as f:
                f.write(log_message)
        except asyncio.TimeoutError:
            logging.warning("Engine communication timeout")
            break
        except Exception as e:
            logging.error(f"Engine communication error: {e}")
            break


async def client_handler(reader, writer, engine_path, log_file, engine_name):
    client_ip = writer.get_extra_info('peername')[0]
    logging.info(f"Connection opened from {client_ip}")
    print(f"Connection opened from {client_ip}")
    
    if config.get("enable_trusted_sources", False):
        # Check if the client IP belongs to any of the trusted subnets or trusted sources
        is_trusted_subnet = any(
            ipaddress.ip_address(client_ip) in ipaddress.ip_network(subnet)
            for subnet in config["trusted_subnets"]
        )
        is_trusted_source = client_ip in config["trusted_sources"]

        if not (is_trusted_subnet or is_trusted_source):
            logging.warning(f"Untrusted connection attempt from {client_ip}")
            check_connection_attempts(client_ip)  # Log the untrusted connection attempt
            writer.close()
            return

    last_activity_time = time.time()  # Initialize last activity time

    async def check_inactivity():
        nonlocal last_activity_time
        while True:
            await asyncio.sleep(60)  # Check every minute
            if time.time() - last_activity_time > 900:  # 15 minutes of inactivity
                logging.warning(f"Connection to {client_ip} closed due to inactivity.")
                writer.close()
                return

    inactivity_task = asyncio.create_task(check_inactivity())

    # Check if the client IP belongs to any of the trusted subnets or trusted sources
    is_trusted_subnet = any(
        ipaddress.ip_address(client_ip) in ipaddress.ip_network(subnet)
        for subnet in config["trusted_subnets"]
    )
    is_trusted_source = client_ip in config["trusted_sources"]

    if not (is_trusted_subnet or is_trusted_source):
        logging.warning(f"Untrusted connection attempt from {client_ip}")
        print(f"Untrusted connection attempt from {client_ip}")
        writer.close()
        await writer.wait_closed()
        logging.info(f"Connection closed for untrusted source {client_ip}")
        print(f"Connection closed for untrusted source {client_ip}")
        return

    async with sem:
        try:
            engine_dir = os.path.dirname(engine_path)
            logging.info(f"Initiating engine {engine_path} for client {client_ip}")
            print(f"Initiating engine {engine_path} for client {client_ip}")

            engine_process = await asyncio.create_subprocess_exec(
                engine_path,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=engine_dir
            )
            
            # Start heartbeat
            heartbeat_task = asyncio.create_task(heartbeat(writer, 300))

            async def process_command(command):
                try:
                    engine_process.stdin.write(f"{command}\n".encode())
                    await engine_process.stdin.drain()
                    if config["enable_uci_log"]:
                        with open(log_file, "a") as f:
                            f.write(f"Client: {command}\n")
                    if config["detailed_log_verbosity"]:
                        print(f"Client: {command}")
                except Exception as e:
                    logging.error(f"Error processing command: {e}")

            async def process_uci_command():
                await process_command("uci")

                # Send all custom UCI options
                for option_name, option_value in ENGINES[engine_name].get("custom_variables", {}).items():
                    await process_command(f"setoption name {option_name} value {option_value}")

                while True:
                    data = await engine_process.stdout.readline()
                    if not data:
                        break
                    decoded_data = data.decode().strip()
                    writer.write(data)
                    await writer.drain()
                    if config["enable_uci_log"]:
                        with open(log_file, "a") as f:
                            f.write(f"Engine: {decoded_data}\n")
                    if config["detailed_log_verbosity"]:
                        print(f"Engine: {decoded_data}")
                    if "uciok" in decoded_data:
                        break

            await process_uci_command()

            async def process_client_commands():
                while True:
                    data = await reader.readline()
                    if not data:
                        break

                    client_data = data.decode().strip()
                    commands = client_data.split('\n')

                    for command in commands:
                        command = command.strip()
                        if command:
                            if command.startswith('setoption name'):
                                parts = command.split(' ')
                                if len(parts) >= 5 and parts[1] == 'name' and parts[3] == 'value':
                                    option_name = parts[2]
                                    option_value = ' '.join(parts[4:])
                                    if option_name in ENGINES[engine_name].get("custom_variables", {}):
                                        if ENGINES[engine_name]["custom_variables"][option_name] == "override":
                                            await process_command(command)
                                        else:
                                            modified_command = f"setoption name {option_name} value {ENGINES[engine_name]['custom_variables'][option_name]}"
                                            await process_command(modified_command)
                                    elif option_name in CUSTOM_VARIABLES:
                                        modified_command = f"setoption name {option_name} value {CUSTOM_VARIABLES[option_name]}"
                                        await process_command(modified_command)
                                    else:
                                        await process_command(command)
                                else:
                                    await process_command(command)
                            else:
                                await process_command(command)

            async def process_engine_responses():
                while True:
                    data = await engine_process.stdout.readline()
                    if not data:
                        break
                    decoded_data = data.decode().strip()
                    writer.write(data)
                    await writer.drain()
                    if config["enable_uci_log"]:
                        with open(log_file, "a") as f:
                            f.write(f"Engine: {decoded_data}\n")
                    if config["detailed_log_verbosity"]:
                        print(f"Engine: {decoded_data}")

            await asyncio.gather(process_client_commands(), process_engine_responses())

        except ConnectionResetError as e:
            logging.info(f"Client {client_ip} disconnected: {e}")
            print(f"Client {client_ip} disconnected")
        except asyncio.IncompleteReadError as e:
            logging.info(f"Client {client_ip} disconnected: {e}")
            print(f"Client {client_ip} disconnected")
        except asyncio.TimeoutError as e:
            logging.warning(f"Connection timeout for client {client_ip}: {e}")
            print(f"Connection timeout for client {client_ip}")
        except Exception as e:
            logging.error(f"Error in client_handler for client {client_ip}: {e}")
            print(f"Error in client_handler for client {client_ip}: {e}")
        finally:
            inactivity_task.cancel()
            heartbeat_task.cancel()
            try:
                engine_process.terminate()
                await engine_process.wait()
            except ProcessLookupError as e:
                logging.warning(f"ProcessLookupError occurred while terminating the engine process for client {client_ip}: {e}")
                print(f"ProcessLookupError occurred while terminating the engine process for client {client_ip}")
            if not writer.is_closing():
                writer.close()
                try:
                    await writer.wait_closed()
                except ConnectionResetError as e:
                    logging.warning(f"ConnectionResetError occurred while closing the connection for client {client_ip}: {e}")
                    pass
            logging.info(f"Connection closed for client {client_ip}")
            print(f"Connection closed for client {client_ip}")


async def start_server(host, port, engine_path, log_file, engine_name):
    retries = 5  # Set a retry limit
    while retries > 0:
        try:
            server = await asyncio.start_server(
                lambda r, w: client_handler(r, w, engine_path, log_file, engine_name),
                host, port)

            addr = server.sockets[0].getsockname()
            logging.info(f"Server listening on {addr} for engine {engine_path}")

            async with server:
                await server.serve_forever()
            break  # Exit loop if server started successfully

        except asyncio.CancelledError:
            logging.info("Server shutdown initiated")
            break
        except Exception as e:
            retries -= 1
            logging.error(f"Error starting server for engine {engine_name}: {e}")
            if retries > 0:
                logging.info("Retrying in 5 seconds...")
                await asyncio.sleep(5)
            else:
                logging.error("Maximum retries reached. Exiting...")
                break


async def main():
    BASE_LOG_DIR = "LOG"  # Initialize with a default value

    await configure_firewall(config)

    if config["enable_server_log"] or config["enable_uci_log"]:
        if not os.path.exists(BASE_LOG_DIR):
            script_dir = os.path.dirname(os.path.abspath(__file__))
            log_dir = os.path.join(script_dir, "LOG")
            os.makedirs(log_dir, exist_ok=True)
            BASE_LOG_DIR = log_dir
        else:
            os.makedirs(BASE_LOG_DIR, exist_ok=True)

    tasks = []
    for engine_name, details in ENGINES.items():
        log_file = os.path.join(BASE_LOG_DIR, f"communication_log_{engine_name}.txt")
        task = asyncio.create_task(start_server(HOST, details["port"], details["path"], log_file, engine_name))
        tasks.append(task)
        logging.info(f"Started server for {engine_name} on port {details['port']}")

    # Start watchdog timer
    watchdog_task = asyncio.create_task(watchdog_timer(300))  # Check every 5 minutes

    # Set up signal handlers for graceful shutdown
    shutdown_event = asyncio.Event()

    def signal_handler():
        logging.info("Shutdown signal received")
        shutdown_event.set()

    signal.signal(signal.SIGINT, lambda *_: signal_handler())
    signal.signal(signal.SIGTERM, lambda *_: signal_handler())

    try:
        await shutdown_event.wait()
    except KeyboardInterrupt:
        logging.info("Server shutdown initiated by user")
    except asyncio.CancelledError:
        pass

    logging.info("Initiating graceful shutdown...")

    # Cancel all tasks
    tasks.append(watchdog_task)  # Add watchdog_task to the list of tasks
    for task in tasks:
        task.cancel()

    # Wait for all tasks to complete cancellation
    await asyncio.gather(*tasks, return_exceptions=True)

    logging.info("Server shutdown completed")

if __name__ == "__main__":
    asyncio.run(main())
