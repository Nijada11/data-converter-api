# Deploying to Azure

This pushes the image to Azure Container Registry (ACR) and runs it on Azure Container Apps. You don't need Docker locally, because `az acr build` builds the image in Azure.

## Costs

Check the current terms before you start, as they change.

- **Azure for Students:** Microsoft lists $100 of credit for 12 months with no credit card. The subscription is disabled when the credit runs out or the 12 months end, and you can renew while you're still a student. See the [offer page](https://azure.microsoft.com/free/students/) and [Microsoft Learn](https://learn.microsoft.com/azure/education-hub/azure-dev-tools-teaching/azure-students-program).
- **Container Apps:** the Consumption plan includes a monthly free grant, and an app scaled to zero has no usage charges. See [pricing](https://azure.microsoft.com/pricing/details/container-apps/).
- **Container Registry:** this is the part that bills while idle, because the registry is charged whether or not the app runs. Check [ACR pricing](https://azure.microsoft.com/pricing/details/container-registry/) so you know what it will take from your credit.

If you add a credit card to the account, the spending limit can be lifted, so charges could become real. Don't do this for a demo project.

## 1. Set a budget alert first

In the Azure portal, go to **Cost Management + Billing**, open **Budgets**, and add one:

- Scope: your subscription
- Amount: a small monthly figure, for example $5
- Alerts: at 50% and 90% of actual cost, sent to your email

If a Student subscription doesn't offer Budgets, watch the credit balance on the [Microsoft Azure Sponsorships](https://www.microsoftazuresponsorships.com/) page instead.

## 2. Sign in and prepare

Install the [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli), then:

```bash
az login
az extension add --name containerapp --upgrade
az provider register --namespace Microsoft.App
az provider register --namespace Microsoft.OperationalInsights
az provider register --namespace Microsoft.ContainerRegistry

RG=data-converter-rg
LOCATION=germanywestcentral    # Student subscriptions may restrict regions
ACR=dataconverter$RANDOM       # Must be globally unique, letters and digits only
ENVIRONMENT=data-converter-env
APP=data-converter-api
API_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
echo "$API_KEY"                # Save this in a password manager

az group create --name $RG --location $LOCATION
```

## 3. Build the image in ACR

Run this from the repository root:

```bash
az acr create --resource-group $RG --name $ACR --sku Basic
az acr build --registry $ACR --image $APP:v1 .
```

## 4. Deploy

```bash
az containerapp env create --name $ENVIRONMENT --resource-group $RG --location $LOCATION

az containerapp create \
  --name $APP \
  --resource-group $RG \
  --environment $ENVIRONMENT \
  --image $ACR.azurecr.io/$APP:v1 \
  --registry-server $ACR.azurecr.io \
  --registry-identity system \
  --secrets api-key="$API_KEY" \
  --env-vars API_KEY=secretref:api-key \
  --ingress external \
  --target-port 8000 \
  --cpu 0.25 --memory 0.5Gi \
  --min-replicas 0 --max-replicas 1
```

`--max-replicas 1` keeps the in-memory rate limit accurate. `--min-replicas 0` scales to zero when idle, so the first request after a quiet spell takes a few seconds.

## 5. Check it

```bash
URL=https://$(az containerapp show --name $APP --resource-group $RG \
  --query properties.configuration.ingress.fqdn --output tsv)
curl "$URL/health"
echo "$URL/docs"
```

Put the `/docs` URL in the README under **Live demo**.

## Deploy a new version

```bash
az acr build --registry $ACR --image $APP:v2 .
az containerapp update --name $APP --resource-group $RG --image $ACR.azurecr.io/$APP:v2
```

## See the logs

```bash
az containerapp logs show --name $APP --resource-group $RG --follow
```

## Remove everything

This deletes the app, the registry and all charges with them:

```bash
az group delete --name $RG
```
