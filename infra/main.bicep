// Phase 7: the hosted Drug Pricing Agent, one template for the whole serving stack in rg-dpa.
//
//   Log Analytics (1 GB/day cap) <- Application Insights      traces, KQL, alert
//   Storage account -> Azure Files share "dpa-data"            Gold snapshot + Price-Watch runs
//   Azure OpenAI -> gpt-4.1-mini deployment                    pinned version, NoAutoUpgrade, capped TPM
//   Container Apps environment -> container app "ca-dpa-web"   UI + Ask agent + MCP at /mcp, scales to zero
//   Managed identity on the app -> "Cognitive Services OpenAI User" on the model account (no API keys)
//   Budget on the resource group (optional, needs alertEmail)
//
// Deploy from Azure Cloud Shell:
//   az deployment group create -g rg-dpa -f infra/main.bicep \
//     -p image=ghcr.io/<owner>/drug-pricing-agent:<tag> deployerPrincipalId=$(az ad signed-in-user show --query id -o tsv) \
//        mcpToken=<random> adminToken=<random> alertEmail=<you>
//
// Idle cost: the app scales to zero; Azure OpenAI bills per token; storage and logs are a few MB.

@description('Short prefix for resource names')
param namePrefix string = 'dpa'

@description('Region for everything except the model account')
param location string = resourceGroup().location

@description('Region for the Azure OpenAI account (model availability differs by region)')
param openAiLocation string = 'eastus2'

@description('Container image for the app, e.g. ghcr.io/owner/drug-pricing-agent:sha-abc1234')
param image string

@description('Model to deploy and its version (check the portal model catalog for what your region offers)')
param modelName string = 'gpt-4.1-mini'
param modelVersion string = '2025-04-14'

@description('Deployment name the agent calls (DPA_MODEL)')
param deploymentName string = 'gpt-4.1-mini'

@allowed(['GlobalStandard', 'DataZoneStandard', 'Standard'])
param deploymentSku string = 'GlobalStandard'

@description('Throughput cap in thousands of tokens per minute. Low on purpose: it bounds the worst-case bill.')
param capacityKTpm int = 30

@description('Object ID of the person deploying (az ad signed-in-user show --query id -o tsv), so evals and Price-Watch can run from Cloud Shell without keys. Empty = skip.')
param deployerPrincipalId string = ''

@secure()
@description('Bearer token for /mcp')
param mcpToken string

@secure()
@description('Token a reviewer needs to approve or reject Price-Watch findings')
param adminToken string

@description('Questions per UTC day across all visitors (caps the model bill)')
param dailyQuestionLimit int = 200

@description('Email for the budget and the grounding alert (empty = skip both)')
param alertEmail string = ''

@description('Monthly budget for the resource group, in the billing currency')
param budgetAmount int = 2000

@description('First day of the budget period (yyyy-MM-01)')
param budgetStartDate string = '${utcNow('yyyy-MM')}-01'

param logDailyCapGb int = 1

var suffix = uniqueString(resourceGroup().id)
var tags = { project: 'drug-pricing-agent', component: 'serving' }
var appName = 'ca-${namePrefix}-web'
var shareName = 'dpa-data'
var openAiUserRole = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd' // Cognitive Services OpenAI User

// ------------------------------------------------------------------ monitoring
resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${namePrefix}-${suffix}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
    workspaceCapping: { dailyQuotaGb: logDailyCapGb }
  }
}

resource appi 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${namePrefix}-${suffix}'
  location: location
  kind: 'other'
  tags: tags
  properties: {
    Application_Type: 'other'
    WorkspaceResourceId: law.id
    IngestionMode: 'LogAnalytics'
    RetentionInDays: 30
  }
}

// ------------------------------------------------------------------ data share
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'st${namePrefix}${suffix}'
  location: location
  tags: tags
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
  }
}

resource files 'Microsoft.Storage/storageAccounts/fileServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource share 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  parent: files
  name: shareName
  properties: { shareQuota: 5 }
}

// ------------------------------------------------------------------ model
resource aoai 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: 'aoai-${namePrefix}-${suffix}'
  location: openAiLocation
  kind: 'OpenAI'
  sku: { name: 'S0' }
  tags: tags
  properties: {
    customSubDomainName: 'aoai-${namePrefix}-${suffix}'
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: true // no API keys exist: callers use Microsoft Entra ID (managed identity / az login)
  }
}

resource deployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: aoai
  name: deploymentName
  sku: { name: deploymentSku, capacity: capacityKTpm }
  properties: {
    model: { format: 'OpenAI', name: modelName, version: modelVersion }
    versionUpgradeOption: 'NoAutoUpgrade' // a silent model upgrade would invalidate the eval baseline
  }
}

resource deployerCanCallModel 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(deployerPrincipalId)) {
  name: guid(aoai.id, deployerPrincipalId, openAiUserRole)
  scope: aoai
  properties: {
    principalId: deployerPrincipalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', openAiUserRole)
    principalType: 'User'
  }
}

// ------------------------------------------------------------------ hosting
resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'cae-${namePrefix}-${suffix}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: law.properties.customerId
        sharedKey: law.listKeys().primarySharedKey
      }
    }
    workloadProfiles: [ { name: 'Consumption', workloadProfileType: 'Consumption' } ]
  }
}

resource envShare 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: env
  name: 'dpadata'
  properties: {
    azureFile: {
      accountName: storage.name
      accountKey: storage.listKeys().keys[0].value
      shareName: shareName
      accessMode: 'ReadWrite' // the app writes Price-Watch review decisions
    }
  }
}

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }
  dependsOn: [ deployment ]
  properties: {
    environmentId: env.id
    workloadProfileName: 'Consumption'
    configuration: {
      ingress: {
        external: true
        targetPort: 8080
        transport: 'auto'
        allowInsecure: false
      }
      secrets: [
        { name: 'appi-conn', value: appi.properties.ConnectionString }
        { name: 'mcp-token', value: mcpToken }
        { name: 'admin-token', value: adminToken }
      ]
    }
    template: {
      containers: [
        {
          name: 'web'
          image: image
          resources: { cpu: json('1.0'), memory: '2Gi' }
          env: [
            { name: 'AZURE_OPENAI_ENDPOINT', value: aoai.properties.endpoint }
            { name: 'DPA_MODEL', value: deploymentName }
            { name: 'DPA_GOLD_DIR', value: '/data/gold' }
            { name: 'DPA_PRICEWATCH_DIR', value: '/data/pricewatch_runs' }
            { name: 'DPA_MCP_ALLOWED_HOSTS', value: '${appName}.${env.properties.defaultDomain}' }
            { name: 'DPA_DAILY_QUESTION_LIMIT', value: string(dailyQuestionLimit) }
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', secretRef: 'appi-conn' }
            { name: 'DPA_MCP_TOKEN', secretRef: 'mcp-token' }
            { name: 'DPA_ADMIN_TOKEN', secretRef: 'admin-token' }
          ]
          volumeMounts: [ { volumeName: 'data', mountPath: '/data' } ]
          probes: [
            { type: 'Startup', httpGet: { path: '/healthz', port: 8080 }, periodSeconds: 5, failureThreshold: 36 }
            { type: 'Liveness', httpGet: { path: '/healthz', port: 8080 }, periodSeconds: 30 }
          ]
        }
      ]
      volumes: [ { name: 'data', storageType: 'AzureFile', storageName: envShare.name } ]
      scale: {
        minReplicas: 0 // scale to zero when idle: no compute bill between visits
        maxReplicas: 1 // the in-app question limits are exact with one replica
        rules: [ { name: 'http', http: { metadata: { concurrentRequests: '20' } } } ]
      }
    }
  }
}

resource appCanCallModel 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(aoai.id, app.id, openAiUserRole)
  scope: aoai
  properties: {
    principalId: app.identity.principalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', openAiUserRole)
    principalType: 'ServicePrincipal'
  }
}

// ------------------------------------------------------------------ guardrails on spend and quality
resource budget 'Microsoft.Consumption/budgets@2023-05-01' = if (!empty(alertEmail)) {
  name: '${namePrefix}-monthly-${budgetAmount}'
  properties: {
    category: 'Cost'
    amount: budgetAmount
    timeGrain: 'Monthly'
    timePeriod: { startDate: budgetStartDate }
    notifications: {
      actual80: { enabled: true, operator: 'GreaterThan', threshold: 80, thresholdType: 'Actual', contactEmails: [ alertEmail ] }
      forecast100: { enabled: true, operator: 'GreaterThan', threshold: 100, thresholdType: 'Forecasted', contactEmails: [ alertEmail ] }
    }
  }
}

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = if (!empty(alertEmail)) {
  name: 'ag-${namePrefix}-llmops'
  location: 'global'
  tags: tags
  properties: {
    groupShortName: 'dpa-llmops'
    enabled: true
    emailReceivers: [ { name: 'owner', emailAddress: alertEmail, useCommonAlertSchema: true } ]
  }
}

resource groundingAlert 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = if (!empty(alertEmail)) {
  name: 'alert-${namePrefix}-ungrounded-answers'
  location: location
  tags: tags
  properties: {
    displayName: 'DPA agent: ungrounded numbers above 5% (1h)'
    severity: 2
    enabled: true
    scopes: [ appi.id ]
    evaluationFrequency: 'PT1H'
    windowSize: 'PT1H'
    criteria: {
      allOf: [
        {
          query: 'dependencies | where name == "invoke_agent dpa-ask" | where tostring(customDimensions["dpa.status"]) == "answered" | summarize n = count(), ungrounded = countif(toint(customDimensions["dpa.grounding.ungrounded"]) > 0) | where n >= 20 and 1.0 * ungrounded / n > 0.05'
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: { numberOfEvaluationPeriods: 1, minFailingPeriodsToAlert: 1 }
        }
      ]
    }
    actions: { actionGroups: [ actionGroup.id ] }
  }
}

output appUrl string = 'https://${app.properties.configuration.ingress.fqdn}'
output mcpUrl string = 'https://${app.properties.configuration.ingress.fqdn}/mcp'
output openAiEndpoint string = aoai.properties.endpoint
output storageAccountName string = storage.name
output shareName string = shareName
output appInsightsName string = appi.name
