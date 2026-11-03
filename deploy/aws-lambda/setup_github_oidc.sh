#!/usr/bin/env bash
# Configura en IAM lo que necesita GitHub Actions para desplegar la ingesta sin claves guardadas (OIDC).
# Ejecutar una vez en AWS CloudShell, después de deploy_cloudshell.sh:
#
#   bash setup_github_oidc.sh
#
# 1. Identidad web de GitHub (token.actions.githubusercontent.com, audiencia sts.amazonaws.com), si no existe.
# 2. Rol github-capacidadbus-deploy: solo lo puede asumir la rama main de MoveIU_BackEnd.
#    Permisos: PowerUserAccess y una política para crear o cambiar solo los roles de la pila capacidadbus-ingesta-*.
# 3. Imprime el ARN del rol y las variables de GitHub, tomadas de la pila ya desplegada.
set -euo pipefail
REPO=${REPO:-OscarHernandezVel/MoveIU_BackEnd}
ROLE=github-capacidadbus-deploy
STACK=capacidadbus-ingesta
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
PROVIDER_ARN="arn:aws:iam::$ACCOUNT:oidc-provider/token.actions.githubusercontent.com"

if aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$PROVIDER_ARN" >/dev/null 2>&1; then
  echo "Identidad web de GitHub: ya existía."
else
  aws iam create-open-id-connect-provider --url https://token.actions.githubusercontent.com \
    --client-id-list sts.amazonaws.com --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1 >/dev/null
  echo "Identidad web de GitHub: creada."
fi

TRUST=$(cat <<JSON
{"Version": "2012-10-17", "Statement": [{"Effect": "Allow",
  "Principal": {"Federated": "$PROVIDER_ARN"},
  "Action": "sts:AssumeRoleWithWebIdentity",
  "Condition": {"StringEquals": {
    "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
    "token.actions.githubusercontent.com:sub": "repo:$REPO:ref:refs/heads/main"}}}]}
JSON
)
if aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  aws iam update-assume-role-policy --role-name "$ROLE" --policy-document "$TRUST"
  echo "Rol $ROLE: ya existía, confianza actualizada."
else
  aws iam create-role --role-name "$ROLE" --assume-role-policy-document "$TRUST" \
    --description "GitHub Actions de $REPO (rama main) despliega la ingesta" >/dev/null
  echo "Rol $ROLE: creado."
fi
# ponytail: PowerUserAccess es amplio; aceptable para el piloto, en producción limitarlo a los servicios de template.yaml.
aws iam attach-role-policy --role-name "$ROLE" --policy-arn arn:aws:iam::aws:policy/PowerUserAccess
aws iam put-role-policy --role-name "$ROLE" --policy-name pila-capacidadbus-ingesta --policy-document "$(cat <<JSON
{"Version": "2012-10-17", "Statement": [{"Effect": "Allow",
  "Action": ["iam:CreateRole", "iam:DeleteRole", "iam:GetRole", "iam:PassRole", "iam:TagRole", "iam:UntagRole",
             "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:GetRolePolicy",
             "iam:AttachRolePolicy", "iam:DetachRolePolicy"],
  "Resource": "arn:aws:iam::$ACCOUNT:role/$STACK-*"}]}
JSON
)"
ROLE_ARN=$(aws iam get-role --role-name "$ROLE" --query Role.Arn --output text)

param() {
  aws cloudformation describe-stacks --stack-name "$STACK" \
    --query "Stacks[0].Parameters[?ParameterKey=='$1'].ParameterValue | [0]" --output text
}
echo
echo "Listo. Copie esto en GitHub ($REPO -> Settings -> Secrets and variables -> Actions):"
echo
echo "  Secreto   AWS_DEPLOY_ROLE_ARN = $ROLE_ARN"
echo "  Variable  AWS_REGION          = ${AWS_REGION:-us-east-1}"
echo "  Variable  BACKEND_URL         = $(param BackendUrl)"
echo "  Variable  VPC_ID              = $(param VpcId)"
echo "  Variable  SUBNET_IDS          = $(param SubnetIds)"
echo "  Variable  BACKEND_SG_ID       = $(param BackendSecurityGroupId)"
echo
echo "EC2_INSTANCE_ID y DB_INSTANCE_ID solo si quiere el apagado nocturno."
