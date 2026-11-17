//! The provider questions: `provider.capabilities`, `provider.models` and
//! `provider.model`. They take a provider config, not an open agent, so a
//! client can ask before it opens one.

use linch::{JsonValue, ProviderConfig};

use super::{Server, to_value};
use crate::error::ErrorObject;
use crate::protocol::{ProviderModelParams, ProviderModels, ProviderParams};

impl Server {
    /// A tenant's connection must be allowed to ask, and to reach the
    /// provider; a local one is not checked.
    fn admit_provider_info(&self, provider: &ProviderConfig) -> Result<(), ErrorObject> {
        match &self.0.tenant {
            Some(tenant) => tenant.admit_provider_info(provider),
            None => Ok(()),
        }
    }

    /// What the provider supports for the model. It sends nothing but the
    /// one-time window probe of a self-hosted server, and needs no key.
    pub(super) async fn provider_capabilities(
        &self,
        params: ProviderModelParams,
    ) -> Result<JsonValue, ErrorObject> {
        self.admit_provider_info(&params.provider)?;
        let provider =
            (params.provider.build_keyless()).map_err(|error| ErrorObject::from(&error))?;
        provider.prepare().await;
        to_value(&provider.capabilities(&params.model))
    }

    /// The models the provider's server lists.
    pub(super) async fn provider_models(
        &self,
        params: ProviderParams,
    ) -> Result<JsonValue, ErrorObject> {
        self.admit_provider_info(&params.provider)?;
        let provider = (params.provider.build()).map_err(|error| ErrorObject::from(&error))?;
        let models = (provider.list_models().await).map_err(|error| ErrorObject::from(&error))?;
        to_value(&ProviderModels { models })
    }

    /// One model of the list.
    pub(super) async fn provider_model(
        &self,
        params: ProviderModelParams,
    ) -> Result<JsonValue, ErrorObject> {
        self.admit_provider_info(&params.provider)?;
        let provider = (params.provider.build()).map_err(|error| ErrorObject::from(&error))?;
        let info = provider
            .model_info(&params.model)
            .await
            .map_err(|error| ErrorObject::from(&error))?;
        to_value(&info)
    }
}
