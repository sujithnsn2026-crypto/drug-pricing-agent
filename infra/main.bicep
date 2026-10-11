// LLMOps infrastructure for the Ask agent, deployed into the existing resource group (rg-dpa).
//
//   Log Analytics workspace  <- Application Insights (workspace-based): traces, KQL, alerts
//   Azure OpenAI account     -> one model deployment the agent calls
//   Scheduled log alert      -> fires when answers with ungrounded numbers exceed 5% in an hour
//
// Deploy:
//   az deployment group create -g rg-dpa -f infra/main.bicep -p alertEmail=you@example.com
// (or compile with `bicep build` and use "Deploy a custom template" in the portal: infra/main.json)
//
// Cost notes: Log Analytics and App Insights bill per GB ingested (a daily cap is set below);
// Azure OpenAI Standard/GlobalStandard bills per token only, with no idle cost.

@description('Short prefix for resource names')
param namePrefix string = 'dpa'

@description('Region for monitoring resources')
param location string = resourceGroup().location

@description('Region for the Azure OpenAI account (model availability differs by region)')
param openAiLocation string = location

@description('Model to deploy, and its version (check the portal model catalog for what your region offers)')
param modelName string = 'gpt-4.1-mini'
param modelVersion string = '2025-04-14'

@description('Deployment name the agent passes as --model')
param deploymentName string = 'gpt-4.1-mini'

@allowed(['GlobalStandard', 'DataZoneStandard', 'Standard'])
param deploymentSku string = 'GlobalStandard'

@description('Throughput limit in thousands of tokens per minute (TPM). Small on purpose: caps runaway spend.')
param capacityKTpm int = 30

@description('Daily ingestion cap for Log Analytics, in GB')
param logDailyCapGb int = 1

@description('Email for the grounding alert (leave empty to skip the alert)')
param alertEmail string = ''

var suffix = uniqueString(resourceGroup().id)
var tags = { project: 'drug-pricing-agent', component: 'llmops' }

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

resource aoai 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: 'aoai-${namePrefix}-${suffix}'
  location: openAiLocation
  kind: 'OpenAI'
  sku: { name: 'S0' }
  tags: tags
  properties: {
    customSubDomainName: 'aoai-${namePrefix}-${suffix}'
    publicNetworkAccess: 'Enabled'
    // Keys stay enabled so the CLI and GitHub Actions work with a secret. For production, set
    // disableLocalAuth: true and use a managed identity (the client supports Entra ID already).
    disableLocalAuth: false
  }
}

resource deployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: aoai
  name: deploymentName
  sku: { name: deploymentSku, capacity: capacityKTpm }
  properties: {
    model: { format: 'OpenAI', name: modelName, version: modelVersion }
    versionUpgradeOption: 'NoAutoUpgrade' // a silent model upgrade would invalidate eval baselines
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

output openAiEndpoint string = aoai.properties.endpoint
output openAiAccountName string = aoai.name
output deploymentName string = deployment.name
output appInsightsName string = appi.name
// The connection string is not a secret-grade credential (it only allows sending telemetry),
// but it is still better copied from the portal than printed into deployment history.
