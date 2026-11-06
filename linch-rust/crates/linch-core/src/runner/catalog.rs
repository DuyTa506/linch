//! The runner's side of the session catalog (see [`crate::catalog`]): the
//! request built on the frozen prefix, and the notice of a change.

use super::turn::Runner;
use crate::catalog;
use crate::message::{ContentBlock, Message};
use crate::provider::ProviderRequest;

impl Runner {
    /// The notice to add to the user message this turn ends with, when
    /// the catalog changed since the model was last told; `None` when it
    /// did not, or before the session has a prefix. A version the run
    /// already checked is not diffed again.
    pub(super) fn catalog_change(&mut self) -> Option<ContentBlock> {
        let catalog = self.catalog.as_ref()?;
        let version = catalog::version(&self.agent);
        if self.seen == Some(version) {
            return None;
        }
        self.seen = Some(version);
        let change = catalog.change(&catalog::live(&self.agent))?;
        Some(ContentBlock::CatalogChange(change))
    }

    /// The request for `messages`: the session's frozen system blocks and
    /// tools, narrowed to this turn's tools when a skill set them. Before
    /// the prefix is logged (never, once the prompt is in), the live ones.
    pub(super) fn request(&self, messages: Vec<Message>) -> ProviderRequest {
        let agent = &self.agent;
        let Some(catalog) = &self.catalog else {
            let system = agent.system();
            return agent.request(system, self.snapshot.definitions(), messages);
        };
        let snapshot = &self.snapshot;
        let keep = |name: &str| snapshot.resolve(name).is_some();
        let keep: Option<&dyn Fn(&str) -> bool> = self.narrowed.then_some(&keep);
        let tools = catalog.tools(keep);
        agent.request(catalog.system().to_vec(), tools, messages)
    }
}
