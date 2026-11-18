use std::num::NonZeroU32;
use std::sync::Arc;
use std::time::Duration;

use tokio::sync::Mutex;
use tokio_util::sync::CancellationToken;

use super::ProviderGate;

fn gate(max: u32) -> ProviderGate {
    ProviderGate::new(NonZeroU32::new(max).unwrap())
}

/// Clones share one budget.
#[tokio::test]
async fn clones_share_one_budget() {
    let gate = gate(2);
    let other = gate.clone();
    let first = gate.try_acquire().unwrap();
    let _second = other.try_acquire().unwrap();
    assert!(gate.try_acquire().is_none());
    assert_eq!((gate.in_flight(), other.max().get()), (2, 2));
    drop(first);
    assert!(other.try_acquire().is_some());
}

/// Waiters get their place in the order they asked.
#[tokio::test]
async fn waiters_are_served_first_in_first_out() {
    let gate = gate(1);
    let held = gate.try_acquire().unwrap();
    let order = Arc::new(Mutex::new(Vec::new()));
    let mut tasks = Vec::new();
    for n in 0..4 {
        let (gate, order) = (gate.clone(), Arc::clone(&order));
        tasks.push(tokio::spawn(async move {
            let _place = gate.acquire(&CancellationToken::new()).await.unwrap();
            order.lock().await.push(n);
        }));
        // Let each waiter queue before the next asks.
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    drop(held);
    for task in tasks {
        task.await.unwrap();
    }
    assert_eq!(*order.lock().await, [0, 1, 2, 3]);
}

/// A waiter that is cancelled takes no place, and the next one is served.
#[tokio::test]
async fn a_cancelled_waiter_leaves_the_queue_without_a_place() {
    let gate = gate(1);
    let held = gate.try_acquire().unwrap();
    let cancel = CancellationToken::new();
    let waiter = {
        let (gate, cancel) = (gate.clone(), cancel.clone());
        tokio::spawn(async move { gate.acquire(&cancel).await.is_none() })
    };
    tokio::time::sleep(Duration::from_millis(20)).await;
    cancel.cancel();
    assert!(waiter.await.unwrap(), "a cancelled wait gives no place");
    drop(held);
    assert_eq!(gate.in_flight(), 0);
    assert!(gate.acquire(&CancellationToken::new()).await.is_some());
}

/// A weak handle does not keep the gate, and a permit does.
#[tokio::test]
async fn a_weak_handle_lives_as_long_as_a_clone_or_a_permit() {
    let gate = gate(1);
    let weak = gate.downgrade();
    assert!(weak.upgrade().is_some());
    let permit = gate.try_acquire().unwrap();
    drop(gate);
    assert!(weak.upgrade().is_some(), "a running call keeps its gate");
    drop(permit);
    assert!(weak.upgrade().is_none());
}
