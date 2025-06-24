#!/bin/bash

# Define color codes
if command -v tput >/dev/null && [ -t 1 ]; then
    RED=$(tput setaf 1)
    GREEN=$(tput setaf 2)
    YELLOW=$(tput setaf 3)
    BLUE=$(tput setaf 4)
    NC=$(tput sgr0) # No Color
else
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[0;33m'
    BLUE='\033[0;34m'
    NC='\033[0m' # No Color
fi

dotenv_replace() {
    local env_var_name=$1
    local new_value=$2
    local file_path=$3
    local sed_option=""

    # Check if running on macOS and adjust sed_option accordingly
    if [[ "$OSTYPE" == "darwin"* ]]; then
        sed_option="-i ''"
    else
        sed_option="-i"
    fi

    # Use eval to correctly handle the dynamic insertion of the sed option
    delimiter="#"
    eval sed $sed_option "s$delimiter^${env_var_name}=.*$delimiter${env_var_name}=${new_value}$delimiter" $file_path
}

echo -e "${YELLOW}Creating .env...${NC}"

# If .env exists, ask user if they want to overwrite it
if [ -f .env ]; then
    read -p "A .env file already exists. Do you want to overwrite it? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo -e "${YELLOW}Exiting...${NC}"
        exit 0
    fi
fi

# Create .env file

if [ ! -e ".env.example" ] ; then
  echo "${RED}No .env.example file found in current directory: $(pwd). Please download .env.example from the Tracecat GitHub repo and rerun the env.sh script."
  exit 1
fi
env_file=".env"

if ! openssl --help &> /dev/null
then
  echo  -e "${RED}Could not run openssl. Please check if openssl is correctly installed."
  exit 1
fi

echo -e "${YELLOW}Generating new service key and signing secret...${NC}"

service_key=$(openssl rand -hex 32)
signing_secret=$(openssl rand -hex 32)


echo -e "${YELLOW}Generating a Fernet encryption key for the database...${NC}"

# Use a cross-platform base64 command (works on both Linux and macOS)
db_fernet_key=$(openssl rand 32 | base64 | tr -d '\n' | tr '+/' '-_')

echo -e "${YELLOW}Creating new .env from .env.example...${NC}"
cp .env.example .env

# Replace existing values of TRACECAT__SERVICE_KEY and TRACECAT__SIGNING_SECRET
dotenv_replace "TRACECAT__SERVICE_KEY" "$service_key" "$env_file"
dotenv_replace "TRACECAT__SIGNING_SECRET" "$signing_secret" "$env_file"
dotenv_replace "TRACECAT__DB_ENCRYPTION_KEY" "$db_fernet_key" "$env_file"


# Prompt user for environment mode
while true; do
    read -p "Use production mode? (y/n, default: y): " prod_mode
    prod_mode=${prod_mode:-y}
    case $prod_mode in
        [Yy]* )
            env_mode="production"
            break
            ;;
        [Nn]* )
            env_mode="development"
            break
            ;;
        * ) echo -e "${RED}Please answer y or n.${NC}";;
    esac
done

# Prompt user for new IP address and strip http:// or https://

while true; do
    read -p "Set \`PUBLIC_APP_URL\` environment variable to (default: localhost): " new_ip
    new_ip=$(sed -E 's/^\s*.*:\/\///g' <<< $new_ip)
    new_ip=${new_ip:-localhost}

    if [ "$new_ip" != "0.0.0.0" ]; then
        break
    fi
    echo -e "${RED}Cannot use 0.0.0.0 as address.\nSee https://docs.tracecat.com/self-hosting/deployment-options/docker-compose#download-configuration-files ${NC}"
done


# Prompt user for PostgreSQL SSL mode
while true; do
    read -p "Require PostgreSQL SSL mode? (y/n, default: n): " postgres_ssl
    postgres_ssl=${postgres_ssl:-n}
    case $postgres_ssl in
        [Yy]* )
            ssl_mode="require"
            break
            ;;
        [Nn]* )
            ssl_mode="disable"
            break
            ;;
        * ) echo -e "${RED}Please answer y or n.${NC}";;
    esac
done

# Prompt user for superadmin email
echo -e "${YELLOW}Setting up first user (superadmin)...${NC}"
while true; do
    read -p "Enter email address for the first user (superadmin): " superadmin_email
    if [[ -z "$superadmin_email" ]]; then
        echo -e "${RED}Email address cannot be empty. Please enter a valid email address.${NC}"
        continue
    fi
    # Basic email validation
    if [[ "$superadmin_email" =~ ^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$ ]]; then
        break
    else
        echo -e "${RED}Please enter a valid email address.${NC}"
    fi
done

# Update environment variables
dotenv_replace "TRACECAT__APP_ENV" "$env_mode" "$env_file"
dotenv_replace "NODE_ENV" "$env_mode" "$env_file"
dotenv_replace "NEXT_PUBLIC_APP_ENV" "$env_mode" "$env_file"
dotenv_replace "PUBLIC_API_URL" "http://${new_ip}/api/" "$env_file"
dotenv_replace "PUBLIC_APP_URL" "http://${new_ip}" "$env_file"
dotenv_replace "TRACECAT__DB_SSLMODE" "$ssl_mode" "$env_file"
dotenv_replace "TRACECAT__AUTH_SUPERADMIN_EMAIL" "$superadmin_email" "$env_file"

# Remove duplicate entries and leading/trailing commas
new_origins=$(echo "$new_origins" | tr ',' '\n' | sort -u | tr '\n' ',' | sed 's/^,//;s/,$//')
dotenv_replace "TRACECAT__ALLOW_ORIGINS" "$new_origins" "$env_file"

echo -e "${GREEN}Environment file created successfully.${NC}"
echo -e "${GREEN}First user (superadmin) email set to: ${superadmin_email}${NC}"
