//! Server-stream plumbing: keepalive events while a stream is idle (E2B's `Keepalive-Ping-Interval` header) and
//! the request deadline (`Connect-Timeout-Ms`), which ends the stream but not the work behind it.

use std::pin::Pin;
use std::time::Duration;

use connectrpc::{ConnectError, RequestContext};
use futures::{Stream, StreamExt};
use tokio::time::Instant;

pub fn keepalive_interval(ctx: &RequestContext) -> Option<Duration> {
    let secs: u64 = ctx.header("keepalive-ping-interval")?.to_str().ok()?.trim().parse().ok()?;
    (secs > 0).then(|| Duration::from_secs(secs.min(3600)))
}

pub fn with_keepalive<T: Send + 'static>(
    inner: impl Stream<Item = T> + Send + 'static,
    every: Option<Duration>,
    deadline: Option<std::time::Instant>,
    keepalive: impl Fn() -> T + Send + 'static,
) -> impl Stream<Item = Result<T, ConnectError>> + Send + 'static {
    struct St<T> {
        inner: Pin<Box<dyn Stream<Item = T> + Send>>,
        every: Option<Duration>,
        deadline: Option<Instant>,
        done: bool,
    }
    let st = St {
        inner: Box::pin(inner),
        every,
        deadline: deadline.map(Instant::from_std),
        done: false,
    };
    futures::stream::unfold((st, keepalive), |(mut st, keepalive)| async move {
        if st.done {
            return None;
        }
        let far = Instant::now() + Duration::from_secs(365 * 24 * 3600);
        let idle = st.every.map(|d| Instant::now() + d).unwrap_or(far);
        let dl = st.deadline.unwrap_or(far);
        tokio::select! {
            item = st.inner.next() => item.map(|v| (Ok(v), (st, keepalive))),
            _ = tokio::time::sleep_until(idle) => {
                let k = keepalive();
                Some((Ok(k), (st, keepalive)))
            }
            _ = tokio::time::sleep_until(dl) => {
                st.done = true;
                Some((Err(ConnectError::deadline_exceeded("the request's deadline passed")), (st, keepalive)))
            }
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn keepalives_while_idle_and_deadline_ends_the_stream() {
        let inner = futures::stream::pending::<u32>();
        let s = with_keepalive(
            inner,
            Some(Duration::from_millis(20)),
            Some(std::time::Instant::now() + Duration::from_millis(70)),
            || 0u32,
        );
        let items: Vec<_> = s.collect().await;
        assert!(items.len() >= 3, "{items:?}");
        assert!(items[..items.len() - 1].iter().all(|r| matches!(r, Ok(0))));
        assert!(items.last().unwrap().is_err());
    }

    #[tokio::test]
    async fn passes_items_through_and_ends_with_the_inner_stream() {
        let s = with_keepalive(futures::stream::iter([1u32, 2, 3]), None, None, || 0u32);
        let items: Vec<u32> = s.map(|r| r.unwrap()).collect().await;
        assert_eq!(items, vec![1, 2, 3]);
    }
}
