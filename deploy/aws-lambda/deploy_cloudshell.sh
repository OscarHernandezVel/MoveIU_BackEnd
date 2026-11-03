#!/usr/bin/env bash
# Despliega la ingesta (API Gateway + Lambda) desde AWS CloudShell, que ya trae AWS CLI y SAM CLI:
#
#   git clone https://github.com/OscarHernandezVel/MoveIU_BackEnd.git && cd MoveIU_BackEnd/deploy/aws-lambda
#   bash deploy_cloudshell.sh <IP elástica del EC2>
#
# Descubre a partir de la IP la instancia, su IP privada, la VPC, las subredes y el grupo capacidadbus-ec2.
# Opcional: APAGADO_NOCTURNO=1 apaga el EC2 a las 23:00 (Bogotá); DB_ID=<identificador> también apaga RDS.
set -euo pipefail
cd "$(dirname "$0")"
PUBLIC_IP=${1:?"uso: bash deploy_cloudshell.sh <IP elástica del EC2>"}

read -r INSTANCE PRIVATE_IP VPC SG < <(aws ec2 describe-instances --filters "Name=ip-address,Values=$PUBLIC_IP" \
  --query "Reservations[0].Instances[0].[InstanceId,PrivateIpAddress,VpcId,SecurityGroups[?GroupName=='capacidadbus-ec2'].GroupId|[0]]" \
  --output text)
if [ "$INSTANCE" = "None" ] || [ -z "$INSTANCE" ]; then
  echo "No hay una instancia con la IP $PUBLIC_IP en esta región ($AWS_REGION). Cambie la región arriba a la derecha." >&2
  exit 1
fi
if [ "$SG" = "None" ]; then
  echo "La instancia $INSTANCE no tiene el grupo de seguridad capacidadbus-ec2 (ver §5.1 de la guía)." >&2
  exit 1
fi
SUBNETS=$(aws ec2 describe-subnets --filters "Name=vpc-id,Values=$VPC" --query "Subnets[].SubnetId" --output text | tr '\t' ',')

echo "EC2 $INSTANCE · IP privada $PRIVATE_IP · VPC $VPC · grupo $SG"
echo "Subredes: $SUBNETS"
python3 test_ingest_proxy.py

STOP_EC2=""
[ "${APAGADO_NOCTURNO:-0}" = "1" ] && STOP_EC2=$INSTANCE
sam deploy --stack-name capacidadbus-ingesta --capabilities CAPABILITY_IAM --resolve-s3 \
  --no-confirm-changeset --no-fail-on-empty-changeset \
  --parameter-overrides "BackendUrl=http://$PRIVATE_IP:8080" "VpcId=$VPC" "SubnetIds=$SUBNETS" \
    "BackendSecurityGroupId=$SG" "Ec2InstanceId=$STOP_EC2" "DbInstanceId=${DB_ID:-}"

URL=$(aws cloudformation describe-stacks --stack-name capacidadbus-ingesta \
  --query "Stacks[0].Outputs[?OutputKey=='IngestUrl'].OutputValue" --output text)
echo
echo "IngestUrl: $URL     <- póngala en EDGE_SERVER_URL del portátil (edge\\edge.env)"
# Con una clave falsa el backend debe contestar 401: prueba la cadena completa API Gateway -> Lambda -> EC2.
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$URL/api/v1/internal/telemetry/batch" \
  -H "content-type: application/json" -H "x-ingest-key: clave-falsa" -d "{}")
case $CODE in
  401) echo "Verificación: 401 con clave falsa. La ingesta llega al backend." ;;
  503) echo "Verificación: 503. El Lambda no alcanza el EC2: ¿está encendido y con 'ops.py up' usando la variante AWS?" ;;
  *) echo "Verificación: respuesta $CODE (se esperaba 401)." ;;
esac
