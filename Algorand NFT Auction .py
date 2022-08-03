import hashlib
import json
from algosdk.v2client.algod import AlgodClient
from algosdk import account, mnemonic
from algosdk.v2client import algod
from algosdk.future import transaction 
from typing import List, Any, Optional, Union
from algosdk.future.transaction import Transaction, SignedTransaction
from typing import List, Tuple, Dict, Any, Optional, Union
from base64 import b64decode
import base64
from algosdk import logic
from algosdk import encoding


#AUCTION CONTRACT


from pyteal import *

def approval_program():
    seller_key = Bytes("seller")
    nft_id_key = Bytes("nft_id")
    start_time_key = Bytes("start")
    end_time_key = Bytes("end")
    reserve_amount_key = Bytes("reserve_amount")
    min_bid_increment_key = Bytes("min_bid_inc")
    num_bids_key = Bytes("num_bids")
    lead_bid_amount_key = Bytes("bid_amount")
    lead_bid_account_key = Bytes("bid_account")
    
    
    

    @Subroutine(TealType.none)
    def closeNFTTo(assetID: Expr, account: Expr) -> Expr:
        asset_holding = AssetHolding.balance(
            Global.current_application_address(), assetID
        )
        return Seq(
            asset_holding,
            If(asset_holding.hasValue()).Then(
                Seq(
                    InnerTxnBuilder.Begin(),
                    InnerTxnBuilder.SetFields(
                        {
                            TxnField.type_enum: TxnType.AssetTransfer,
                            TxnField.xfer_asset: assetID,
                            TxnField.asset_close_to: account,
                        }
                    ),
                    InnerTxnBuilder.Submit(),
                )
            ),
        )

    @Subroutine(TealType.none)
    def repayPreviousLeadBidder(prevLeadBidder: Expr, prevLeadBidAmount: Expr) -> Expr:
        return Seq(
            InnerTxnBuilder.Begin(),
            InnerTxnBuilder.SetFields(
                {
                    TxnField.type_enum: TxnType.Payment,
                    TxnField.amount: prevLeadBidAmount - Global.min_txn_fee(),
                    TxnField.receiver: prevLeadBidder,
                }
            ),
            InnerTxnBuilder.Submit(),
        )

    @Subroutine(TealType.none)
    def closeAccountTo(account: Expr) -> Expr:
        return If(Balance(Global.current_application_address()) != Int(0)).Then(
            Seq(
                InnerTxnBuilder.Begin(),
                InnerTxnBuilder.SetFields(
                    {
                        TxnField.type_enum: TxnType.Payment,
                        TxnField.close_remainder_to: account,
                    }
                ),
                InnerTxnBuilder.Submit(),
            )
        )

    on_create_start_time = Btoi(Txn.application_args[2])
    on_create_end_time = Btoi(Txn.application_args[3])
    on_create = Seq(
        App.globalPut(seller_key, Txn.application_args[0]),
        App.globalPut(nft_id_key, Btoi(Txn.application_args[1])),
        App.globalPut(start_time_key, on_create_start_time),
        App.globalPut(end_time_key, on_create_end_time),
        App.globalPut(reserve_amount_key, Btoi(Txn.application_args[4])),
        App.globalPut(min_bid_increment_key, Btoi(Txn.application_args[5])),
        App.globalPut(lead_bid_account_key, Global.zero_address()),
        Assert(
            And(
                Global.latest_timestamp() < on_create_start_time,
                on_create_start_time < on_create_end_time,
                # TODO: should we impose a maximum auction length?
            )
        ),
        Approve(),
    )

    on_setup = Seq(
        Assert(Global.latest_timestamp() < App.globalGet(start_time_key)),
        # opt into NFT asset -- because you can't opt in if you're already opted in, this is what
        # we'll use to make sure the contract has been set up
        InnerTxnBuilder.Begin(),
        InnerTxnBuilder.SetFields(
            {
                TxnField.type_enum: TxnType.AssetTransfer,
                TxnField.xfer_asset: App.globalGet(nft_id_key),
                TxnField.asset_receiver: Global.current_application_address(),
            }
        ),
        InnerTxnBuilder.Submit(),
        Approve(),
    )

    on_bid_txn_index = Txn.group_index() - Int(1)
    on_bid_nft_holding = AssetHolding.balance(
        Global.current_application_address(), App.globalGet(nft_id_key)
    )
    on_bid = Seq(
        on_bid_nft_holding,
        Assert(
            And(
                # the auction has been set up
                on_bid_nft_holding.hasValue(),
                on_bid_nft_holding.value() > Int(0),
                # the auction has started
                App.globalGet(start_time_key) <= Global.latest_timestamp(),
                # the auction has not ended
                Global.latest_timestamp() < App.globalGet(end_time_key),
                # the actual bid payment is before the app call
                Gtxn[on_bid_txn_index].type_enum() == TxnType.Payment,
                Gtxn[on_bid_txn_index].sender() == Txn.sender(),
                Gtxn[on_bid_txn_index].receiver()
                == Global.current_application_address(),
                Gtxn[on_bid_txn_index].amount() >= Global.min_txn_fee(),
            )
        ),
        If(
            Gtxn[on_bid_txn_index].amount()
            >= App.globalGet(lead_bid_amount_key) + App.globalGet(min_bid_increment_key)
        ).Then(
            Seq(
                If(App.globalGet(lead_bid_account_key) != Global.zero_address()).Then(
                    repayPreviousLeadBidder(
                        App.globalGet(lead_bid_account_key),
                        App.globalGet(lead_bid_amount_key),
                    )
                ),
                App.globalPut(lead_bid_amount_key, Gtxn[on_bid_txn_index].amount()),
                App.globalPut(lead_bid_account_key, Gtxn[on_bid_txn_index].sender()),
                App.globalPut(num_bids_key, App.globalGet(num_bids_key) + Int(1)),
                Approve(),
            )
        ),
        Reject(),
    )
    
    
    on_call_method = Txn.application_args[0]
    on_call = Cond(
        [on_call_method == Bytes("setup"), on_setup],
        [on_call_method == Bytes("bid"), on_bid],
    )

    on_delete = Seq(
        If(Global.latest_timestamp() < App.globalGet(start_time_key)).Then(
            Seq(
                # the auction has not yet started, it's ok to delete
                Assert(
                    Or(
                        # sender must either be the seller or the auction creator
                        Txn.sender() == App.globalGet(seller_key),
                        Txn.sender() == Global.creator_address(),
                    )
                ),
                # if the auction contract account has opted into the nft, close it out
                closeNFTTo(App.globalGet(nft_id_key), App.globalGet(seller_key)),
                # if the auction contract still has funds, send them all to the seller
                closeAccountTo(App.globalGet(seller_key)),
                Approve(),
            )
        ),
        If(App.globalGet(end_time_key) <= Global.latest_timestamp()).Then(
            Seq(
                # the auction has ended, pay out assets
                If(App.globalGet(lead_bid_account_key) != Global.zero_address())
                .Then(
                    If(
                        App.globalGet(lead_bid_amount_key)
                        >= App.globalGet(reserve_amount_key)
                    )
                    .Then(
                        # the auction was successful: send lead bid account the nft
                        closeNFTTo(
                            App.globalGet(nft_id_key),
                            App.globalGet(lead_bid_account_key),
                        )
                    )
                    .Else(
                        Seq(
                            # the auction was not successful because the reserve was not met: return
                            # the nft to the seller and repay the lead bidder
                            closeNFTTo(
                                App.globalGet(nft_id_key), App.globalGet(seller_key)
                            ),
                            repayPreviousLeadBidder(
                                App.globalGet(lead_bid_account_key),
                                App.globalGet(lead_bid_amount_key),
                            ),
                        )
                    )
                )
                .Else(
                    # the auction was not successful because no bids were placed: return the nft to the seller
                    closeNFTTo(App.globalGet(nft_id_key), App.globalGet(seller_key))
                ),
                # send remaining funds to the seller
                closeAccountTo(App.globalGet(seller_key)),
                Approve(),
            )
        ),
        Reject(),
    )

    program = Cond(
        [Txn.application_id() == Int(0), on_create],
        [Txn.on_completion() == OnComplete.NoOp, on_call],
        [
            Txn.on_completion() == OnComplete.DeleteApplication,
            on_delete,
        ],
        [
            Or(
                Txn.on_completion() == OnComplete.OptIn,
                Txn.on_completion() == OnComplete.CloseOut,
                Txn.on_completion() == OnComplete.UpdateApplication,
            ),
            Reject(),
        ],
    )

    return compileTeal(program, Mode.Application, version = 5)


def clear_state_program():
    return compileTeal(Approve(), Mode.Application, version = 5)
      
'''
Auction contract ends 

network interactioin functioins starts

'''

class NetworkInteraction:

    @staticmethod
    def wait_for_confirmation(client: algod.AlgodClient, txid):
        """
        Utility function to wait until the transaction is
        confirmed before proceeding.
        """
        last_round = client.status().get('last-round')
        txinfo = client.pending_transaction_info(txid)
        while not (txinfo.get('confirmed-round') and txinfo.get('confirmed-round') > 0):
            print("Waiting for confirmation")
            last_round += 1
            client.status_after_block(last_round)
            txinfo = client.pending_transaction_info(txid)
        print(f"Transaction {txid} confirmed in round {txinfo.get('confirmed-round')}.")
        return txinfo

    @staticmethod
    def get_default_suggested_params(client: algod.AlgodClient):
        """
        Gets default suggested params with flat transaction fee and fee amount of 1000.
        :param client:
        :return:
        """
        suggested_params = client.suggested_params()

        suggested_params.flat_fee = True
        suggested_params.fee = 1000

        return suggested_params

    @staticmethod
    def submit_asa_creation(client: algod.AlgodClient, transaction: SignedTransaction):
        """
        Submits a ASA creation transaction to the network. If the transaction is successful the ASA's id is returned.
        :param client:
        :param transaction:
        :return:
        """
        txid = client.send_transaction(transaction)

        NetworkInteraction.wait_for_confirmation(client, txid)

        try:
            ptx = client.pending_transaction_info(txid)
            return ptx["asset-index"], txid
        except Exception as e:
            # TODO: Proper logging needed.
            print(e)
            print('Unsuccessful creation of Algorand Standard Asset.')

    @staticmethod
    def submit_transaction(client: algod.AlgodClient, transaction: SignedTransaction) -> Optional[str]:
        txid = client.send_transaction(transaction)

        NetworkInteraction.wait_for_confirmation(client, txid)

        return txid

    @staticmethod
    def compile_program(client: algod.AlgodClient, source_code):
        """
        :param client: algorand client
        :param source_code: teal source code
        :return:
            Decoded byte program
        """
        compile_response = client.compile(source_code)
        return base64.b64decode(compile_response['result'])

class PendingTxnResponse:
    def __init__(self, response: Dict[str, Any]) -> None:
        self.poolError: str = response["pool-error"]
        self.txn: Dict[str, Any] = response["txn"]

        self.applicationIndex: Optional[int] = response.get("application-index")
        self.assetIndex: Optional[int] = response.get("asset-index")
        self.closeRewards: Optional[int] = response.get("close-rewards")
        self.closingAmount: Optional[int] = response.get("closing-amount")
        self.confirmedRound: Optional[int] = response.get("confirmed-round")
        self.globalStateDelta: Optional[Any] = response.get("global-state-delta")
        self.localStateDelta: Optional[Any] = response.get("local-state-delta")
        self.receiverRewards: Optional[int] = response.get("receiver-rewards")
        self.senderRewards: Optional[int] = response.get("sender-rewards")

        self.innerTxns: List[Any] = response.get("inner-txns", [])
        self.logs: List[bytes] = [b64decode(l) for l in response.get("logs", [])]


def waitForTransaction(
    client: AlgodClient, txID: str, timeout: int = 10
) -> PendingTxnResponse:
    lastStatus = client.status()
    lastRound = lastStatus["last-round"]
    startRound = lastRound

    while lastRound < startRound + timeout:
        pending_txn = client.pending_transaction_info(txID)

        if pending_txn.get("confirmed-round", 0) > 0:
            return PendingTxnResponse(pending_txn)

        if pending_txn["pool-error"]:
            raise Exception("Pool error: {}".format(pending_txn["pool-error"]))

        lastStatus = client.status_after_block(lastRound + 1)

        lastRound += 1

    raise Exception(
        "Transaction {} not confirmed after {} rounds".format(txID, timeout)
    )


def fullyCompileContract(client: AlgodClient, contract: Expr) -> bytes:
    teal = compileTeal(contract, mode=Mode.Application, version=5)
    response = client.compile(teal)
    return b64decode(response["result"])


def decodeState(stateArray: List[Any]) -> Dict[bytes, Union[int, bytes]]:
    state: Dict[bytes, Union[int, bytes]] = dict()

    for pair in stateArray:
        key = b64decode(pair["key"])

        value = pair["value"]
        valueType = value["type"]

        if valueType == 2:
            # value is uint64
            value = value.get("uint", 0)
        elif valueType == 1:
            # value is byte array
            value = b64decode(value.get("bytes", ""))
        else:
            raise Exception(f"Unexpected state type: {valueType}")

        state[key] = value

    return state


def getAppGlobalState(
    client: AlgodClient, appID: int
) -> Dict[bytes, Union[int, bytes]]:
    appInfo = client.application_info(appID)
    return decodeState(appInfo["params"]["global-state"])


def getBalances(client: AlgodClient, account: str) -> Dict[int, int]:
    balances: Dict[int, int] = dict()

    accountInfo = client.account_info(account)

    # set key 0 to Algo balance
    balances[0] = accountInfo["amount"]

    assets: List[Dict[str, Any]] = accountInfo.get("assets", [])
    for assetHolding in assets:
        assetID = assetHolding["asset-id"]
        amount = assetHolding["amount"]
        balances[assetID] = amount

    return balances


def getLastBlockTimestamp(client: AlgodClient) -> Tuple[int, int]:
    status = client.status()
    lastRound = status["last-round"]
    block = client.block_info(lastRound)
    timestamp = block["block"]["ts"]

    return block, timestamp



def get_algod_client(url, api_key) -> AlgodClient:
    headers = {
        'X-API-Key': api_key
    }
    return AlgodClient(api_key, url, headers)


class ASATransactionRepository:
    """
    Initializes transactions related to Algorand Standard Assets
    """

    @classmethod
    def create_asa(cls,
                   client: algod.AlgodClient,
                   creator_private_key: str,
                   unit_name: str,
                   asset_name: str,
                   total: int,
                   decimals: int,
                   metadata_hash : bytes,
                   note: Optional[bytes] = None,
                   manager_address: Optional[str] = None,
                   reserve_address: Optional[str] = None,
                   freeze_address: Optional[str] = None,
                   clawback_address: Optional[str] = None,
                   url: Optional[str] = None,
                   default_frozen: bool = False,
                   sign_transaction: bool = True) -> Union[Transaction, SignedTransaction]:
        """

        :param client:
        :param creator_private_key:
        :param unit_name:
        :param asset_name:
        :param total:
        :param decimals:
        :param note:
        :param manager_address:
        :param reserve_address:
        :param freeze_address:
        :param clawback_address:
        :param url:
        :param default_frozen:
        :param sign_transaction:
        :return:
        """

        suggested_params = NetworkInteraction.get_default_suggested_params(client=client)

        creator_address = account.address_from_private_key(private_key=creator_private_key)

        txn = transaction.AssetConfigTxn(sender=creator_address,
                                      sp=suggested_params,
                                      total=total,
                                      default_frozen=default_frozen,
                                      unit_name=unit_name,
                                      asset_name=asset_name,
                                      manager=manager_address,
                                      reserve=reserve_address,
                                      freeze=freeze_address,
                                      clawback=clawback_address,
                                      url=url,
                                      metadata_hash = metadata_hash,
                                      decimals=decimals,
                                      note=note)

        if sign_transaction:
            txn = txn.sign(private_key=creator_private_key)

        return txn

    @classmethod
    def create_non_fungible_asa(cls,
                                client: algod.AlgodClient,
                                creator_private_key: str,
                                unit_name: str,
                                asset_name: str,
                                metadata : bytes,
                                url: str,
                                note: Optional[bytes] = None,
                                manager_address: Optional[str] = None,
                                reserve_address: Optional[str] = None,
                                freeze_address: Optional[str] = None,
                                clawback_address: Optional[str] = None,
                                default_frozen: bool = False,
                                sign_transaction: bool = True) -> Union[Transaction, SignedTransaction]:
        """

        :param client:
        :param creator_private_key:
        :param unit_name:
        :param asset_name:
        :param note:
        :param manager_address:
        :param reserve_address:
        :param freeze_address:
        :param clawback_address:
        :param url:
        :param default_frozen:
        :param sign_transaction:
        :return:
        """

        return ASATransactionRepository.create_asa(client=client,
                                                   creator_private_key=creator_private_key,
                                                   unit_name=unit_name,
                                                   asset_name=asset_name,
                                                   total=1,
                                                   decimals=0,
                                                   note=note,
                                                   manager_address=manager_address,
                                                   reserve_address=reserve_address,
                                                   freeze_address=freeze_address,
                                                   clawback_address=clawback_address,
                                                   url=url,
                                                   metadata_hash = metadata,
                                                   default_frozen=default_frozen,
                                                   sign_transaction=sign_transaction)

    @classmethod
    def asa_opt_in(cls,
                   client: algod.AlgodClient,
                   sender_private_key: str,
                   asa_id: int,
                   sign_transaction: bool = True) -> Union[Transaction, SignedTransaction]:
        """
        Opts-in the sender's account to the specified asa with an id: asa_id.
        :param client:
        :param sender_private_key:
        :param asa_id:
        :param sign_transaction:
        :return:
        """

        suggested_params = NetworkInteraction.get_default_suggested_params(client=client)
        sender_address = account.address_from_private_key(sender_private_key)

        txn = transaction.AssetTransferTxn(sender=sender_address,
                                        sp=suggested_params,
                                        receiver=sender_address,
                                        amt=0,
                                        index=asa_id)

        if sign_transaction:
            txn = txn.sign(private_key=sender_private_key)

        return txn

    @classmethod
    def asa_transfer(cls,
                     client: algod.AlgodClient,
                     sender_address: str,
                     receiver_address: str,
                     asa_id: int,
                     amount: int,
                     revocation_target: Optional[str],
                     sender_private_key: Optional[str],
                     sign_transaction: bool = True) -> Union[Transaction, SignedTransaction]:
        """
        :param client:
        :param sender_address:
        :param receiver_address:
        :param asa_id:
        :param amount:
        :param revocation_target:
        :param sender_private_key:
        :param sign_transaction:
        :return:
        """
        suggested_params = NetworkInteraction.get_default_suggested_params(client=client)

        txn = transaction.AssetTransferTxn(sender=sender_address,
                                        sp=suggested_params,
                                        receiver=receiver_address,
                                        amt=amount,
                                        index=asa_id,
                                        revocation_target=revocation_target)

        if sign_transaction:
            txn = txn.sign(private_key=sender_private_key)

        return txn

    @classmethod
    def change_asa_management(cls,
                              client: algod.AlgodClient,
                              current_manager_pk: str,
                              asa_id: int,
                              manager_address: Optional[str] = None,
                              reserve_address: Optional[str] = None,
                              freeze_address: Optional[str] = None,
                              clawback_address: Optional[str] = None,
                              strict_empty_address_check: bool = True,
                              sign_transaction: bool = True) -> Union[Transaction, SignedTransaction]:
        """
        Changes the management properties of a given ASA.
        :param client:
        :param current_manager_pk:
        :param asa_id:
        :param manager_address:
        :param reserve_address:
        :param freeze_address:
        :param clawback_address:
        :param strict_empty_address_check:
        :param sign_transaction:
        :return:
        """

        params = NetworkInteraction.get_default_suggested_params(client=client)

        current_manager_address = account.address_from_private_key(private_key=current_manager_pk)

        txn = transaction.AssetConfigTxn(
            sender=current_manager_address,
            sp=params,
            index=asa_id,
            manager=manager_address,
            reserve=reserve_address,
            freeze=freeze_address,
            clawback=clawback_address,
            strict_empty_address_check=strict_empty_address_check)

        if sign_transaction:
            txn = txn.sign(private_key=current_manager_pk)

        return txn


class PaymentTransactionRepository:

    @classmethod
    def payment(cls,
                client: algod.AlgodClient,
                sender_address: str,
                receiver_address: str,
                amount: int,
                sender_private_key: Optional[str],
                sign_transaction: bool = True) -> Union[Transaction, SignedTransaction]:
        """
        Creates a payment transaction in ALGOs.
        :param client:
        :param sender_address:
        :param receiver_address:
        :param amount:
        :param sender_private_key:
        :param sign_transaction:
        :return:
        """
        suggested_params = NetworkInteraction.get_default_suggested_params(client=client)

        txn = transaction.PaymentTxn(sender=sender_address,
                                  sp=suggested_params,
                                  receiver=receiver_address,
                                  amt=amount)

        if sign_transaction:
            txn = txn.sign(private_key=sender_private_key)

        return txn
"""

 operations end
 NFT minting service starts

"""
class NFTService:
    def __init__(
            self,
            nft_creator_address: str,
            nft_creator_pk: str,
            client,
            unit_name: str,
            asset_name: str,
            nft_url : str,
    ):
        self.nft_creator_address = nft_creator_address
        self.nft_creator_pk = nft_creator_pk
        self.client = client

        self.unit_name = unit_name
        self.asset_name = asset_name
        self.nft_url = nft_url

        self.nft_id = None

# function to create NFT genration transaction
    def create_nft(self,metadatahash):
        signed_txn = ASATransactionRepository.create_non_fungible_asa(
            client=self.client,
            creator_private_key=self.nft_creator_pk,
            unit_name=self.unit_name,
            asset_name=self.asset_name,
            metadata = metadatahash,
            note=None,
            manager_address=self.nft_creator_address,
            reserve_address=self.nft_creator_address,
            freeze_address=self.nft_creator_address,
            clawback_address=self.nft_creator_address,
            url=self.nft_url,
            default_frozen=True,
            sign_transaction=True,
        )

        nft_id, tx_id = NetworkInteraction.submit_asa_creation(
            client=self.client, transaction=signed_txn
        )
        self.nft_id = nft_id
        print (nft_id)
        return tx_id

# function to change credentials of NFT generated
    def change_nft_credentials_txn(self, escrow_address):
        txn = ASATransactionRepository.change_asa_management(
            client=self.client,
            current_manager_pk=self.nft_creator_pk,
            asa_id=self.nft_id,
            manager_address="",
            reserve_address="",
            freeze_address="",
            strict_empty_address_check=False,
            clawback_address=escrow_address,
            sign_transaction=True,
        )

        tx_id = NetworkInteraction.submit_transaction(self.client, transaction=txn)

        return tx_id
        
# function to optin in the created asset which is neccesary 
    def opt_in(self, account_pk):
        opt_in_txn = ASATransactionRepository.asa_opt_in(
            client=self.client, sender_private_key=account_pk, asa_id=self.nft_id
        )

        tx_id = NetworkInteraction.submit_transaction(self.client, transaction=opt_in_txn)
        return tx_id


"""

NFT minting service ends

account obkect defination starts

"""
class Account:
    """Represents a private key and address for an Algorand account"""

    def __init__(self, private_key: str) -> None:
        self.sk = private_key
        self.adr = account.address_from_private_key(private_key)

    def get_address(self) -> str:
        return self.adr

    def get_private_key(self) -> str:
        return self.sk

    def get_mnemonic(self) -> str:
        return mnemonic.from_private_key(self.sk)

    @classmethod
    def from_mnemonic(cls, m: str) -> "Account":
        return cls(mnemonic.to_private_key(m))

"""
Contract interactions functions starts 

(these are callable functions from front end defined to interact with contract efficiently)

"""

def compile_to_teal():
    # compile program to TEAL assembly
    with open("./approval.teal", "w") as f:
        x=approval_program()
        approval_program_teal = x
        f.write(approval_program_teal)

    # compile program to TEAL assembly
    with open("./clear.teal", "w") as f:
        clear_state_program_teal = clear_state_program()
        f.write(clear_state_program_teal)
    return approval_program_teal, clear_state_program_teal

def fully_compile_contract(client: AlgodClient, teal: str) -> bytes:
    response = client.compile(teal)
    return b64decode(response["result"])

def get_contracts(client: AlgodClient) -> Tuple[bytes, bytes]:
    approval_program_teal, clear_state_program_teal = compile_to_teal()
    approval_program = fully_compile_contract(
        client, approval_program_teal)
    clear_state_program = fully_compile_contract(
        client, clear_state_program_teal)

    return approval_program, clear_state_program

def create_app(client: AlgodClient, sender: Account, seller : str, nft_id : int, startTime : int, endTime : int, reserve_ammount : int, min_bid_increment : int ) -> int:
    approval_program, clear_program = get_contracts(client)

    global_schema = transaction.StateSchema(8, 8)
    # num_byte_slice = 16 to store max orders
    local_schema = transaction.StateSchema(0, 0)
    
    txn = transaction.ApplicationCreateTxn(
        sender=sender.get_address(),
        sp=client.suggested_params(),
        on_complete=transaction.OnComplete.NoOpOC.real,
        approval_program=approval_program,
        clear_program=clear_program,
        global_schema=global_schema,
        local_schema=local_schema,
        app_args=[seller, nft_id, startTime, endTime, reserve_ammount, min_bid_increment]
        )
    signed_txn = txn.sign(sender.get_private_key())
    tx_id = client.send_transaction(signed_txn)

    response = waitForTransaction(client, tx_id)
    assert response.applicationIndex is not None and response.applicationIndex > 0
    return response.applicationIndex



def setupAuctionApp(
    client: AlgodClient,
    appID: int,
    funder: Account,
    nftHolder: Account,
    nftID: int,
    nftAmount: int,
) -> None:
    
#     """Finish setting up an auction.

#     This operation funds the app auction escrow account, opts that account into
#     the NFT, and sends the NFT to the escrow account, all in one atomic
#     transaction group. The auction must not have started yet.

#     The escrow account requires a total of 0.203 Algos for funding. See the code
#     below for a breakdown of this amount.

#     Args:
#         client: An algod client.
#         appID: The app ID of the auction.
#         funder: The account providing the funding for the escrow account.
#         nftHolder: The account holding the NFT.
#         nftID: The NFT ID.
#         nftAmount: The NFT amount being auctioned. Some NFTs has a total supply
#             of 1, while others are fractional NFTs with a greater total supply,
#             so use a value that makes sense for the NFT being auctioned.
#     """
    appAddr = logic.get_application_address(appID)

    suggestedParams = client.suggested_params()

    fundingAmount = (
        # min account balance
        100_000
        # additional min balance to opt into NFT
        + 100_000
        # 3 * min txn fee
        + 3 * 1_000
    )

    fundAppTxn = transaction.PaymentTxn(
        sender=funder.get_address(),
        receiver=appAddr,
        amt=fundingAmount,
        sp=suggestedParams,
    )

    setupTxn = transaction.ApplicationCallTxn(
        sender=funder.get_address(),
        index=appID,
        on_complete=transaction.OnComplete.NoOpOC,
        app_args=[b"setup"],
        foreign_assets=[nftID],
        sp=suggestedParams,
    )

    fundNftTxn = transaction.AssetTransferTxn(
        sender=nftHolder.get_address(),
        receiver=appAddr,
        index=nftID,
        amt=nftAmount,
        sp=suggestedParams,
    )

    transaction.assign_group_id([fundAppTxn, setupTxn, fundNftTxn])

    signedFundAppTxn = fundAppTxn.sign(funder.get_private_key())
    signedSetupTxn = setupTxn.sign(funder.get_private_key())
    signedFundNftTxn = fundNftTxn.sign(nftHolder.get_private_key())

    client.send_transactions([signedFundAppTxn, signedSetupTxn, signedFundNftTxn])

    waitForTransaction(client, signedFundAppTxn.get_txid())





def placeBid(client: AlgodClient, appID: int, bidder: Account, bidAmount: int) -> None:
#     """Place a bid on an active auction.

#     Args:
#         client: An Algod client.
#         appID: The app ID of the auction.
#         bidder: The account providing the bid.
#         bidAmount: The amount of the bid.
#     """
    appAddr = logic.get_application_address(appID)
    appGlobalState = getAppGlobalState(client, appID)

    nftID = appGlobalState[b"nft_id"]

    if any(appGlobalState[b"bid_account"]):
        # if "bid_account" is not the zero address
        prevBidLeader = encoding.encode_address(appGlobalState[b"bid_account"])
    else:
        prevBidLeader = None

    suggestedParams = client.suggested_params()

    payTxn = transaction.PaymentTxn(
        sender=bidder.get_address(),
        receiver=appAddr,
        amt=bidAmount,
        sp=suggestedParams,
    )

    appCallTxn = transaction.ApplicationCallTxn(
        sender=bidder.get_address(),
        index=appID,
        on_complete=transaction.OnComplete.NoOpOC,
        app_args=[b"bid"],
        foreign_assets=[nftID],
        # must include the previous lead bidder here to the app can refund that bidder's payment
        accounts=[prevBidLeader] if prevBidLeader is not None else [],
        sp=suggestedParams,
    )

    transaction.assign_group_id([payTxn, appCallTxn])

    signedPayTxn = payTxn.sign(bidder.get_private_key())
    signedAppCallTxn = appCallTxn.sign(bidder.get_private_key())

    client.send_transactions([signedPayTxn, signedAppCallTxn])

    waitForTransaction(client, appCallTxn.get_txid())





def closeAuction(client: AlgodClient, appID: int, closer: Account):
#     """Close an auction.
#     This action can only happen before an auction has begun, in which case it is
#     cancelled, or after an auction has ended.
#     If called after the auction has ended and the auction was successful, the
#     NFT is transferred to the winning bidder and the auction proceeds are
#     transferred to the seller. If the auction was not successful, the NFT and
#     all funds are transferred to the seller.
#     Args:
#         client: An Algod client.
#         appID: The app ID of the auction.
#         closer: The account initiating the close transaction. This must be
#             either the seller or auction creator if you wish to close the
#             auction before it starts. Otherwise, this can be any account.
#     """
    appGlobalState = getAppGlobalState(client, appID)

    nftID = appGlobalState[b"nft_id"]

    accounts: List[str] = [encoding.encode_address(appGlobalState[b"seller"])]

    if any(appGlobalState[b"bid_account"]):
        # if "bid_account" is not the zero address
        accounts.append(encoding.encode_address(appGlobalState[b"bid_account"]))

    deleteTxn = transaction.ApplicationDeleteTxn(
        sender=closer.get_address(),
        index=appID,
        accounts=accounts,
        foreign_assets=[nftID],
        sp=client.suggested_params(),
    )
    signedDeleteTxn = deleteTxn.sign(closer.get_private_key())

    client.send_transaction(signedDeleteTxn)

    waitForTransaction(client, signedDeleteTxn.get_txid())









algod_url = "https://testnet-algorand.api.purestake.io/ps2"
algod_api_key = "c6zlIAzzGE9WldEGPxkt847b0PcR2QYs87aHr5rz"
    
client = get_algod_client(algod_url, algod_api_key)
user1 = {
#    'mnemonic': "strategy device nuclear fan venture produce journey hip possible front weapon ride agent lens finger find strategy little swift valley hand crazy swing absorb clog",
    'mnemonic': "cover bread almost illegal shock yard coast mountain vague believe pupil panic someone group chaos cable nuclear blade current pole ritual grunt kangaroo absent better",
    'address' : "YZA63O5ROROFMGWLEOG7V2CETC2WUO64Q574D7G56ZIAKAOJH45B2O7A5Y",
}

user2 = {

    'mnemonic': "tip fatal stomach poet glow subject genre bundle vital napkin budget recycle will work news sting eagle venture radio smile cigar where jewel above hip",
    'address' : "A7MKKYFAWRPB4YGYE7OX5RUOU6NGBBLHNLHPZ7LIUDYIWC7O546UPSIQDI",
}

admin = Account.from_mnemonic(user1["mnemonic"])
print(admin.get_address())
        
buyer = Account.from_mnemonic(user2["mnemonic"])
print(buyer.get_address())

admin_addr = admin.get_address()
admin_pk = admin.get_private_key()

buyer_addr = buyer.get_address()
buyer_pk = buyer.get_private_key()

#initializint NFTService class which contains nft creation function, change NFT credential function and opt in function 

nft_service = NFTService(nft_creator_address=admin_addr,
                         nft_creator_pk=admin_pk,
                         client=client,
                         nft_url = "https://ipfs.io/ipfs/QmZr8EXf29iBeiJpEJoYFK6izcM4SYHHeqwgC1XVNXr8BC",
                         asset_name="Algobot",
                         unit_name="Algobot")

# this is the metadata which needs to be passed in nft creation function 
info = {
    "name": "PYTHONZ",
    "description": "DEV's Artwork",
    "image": "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcQzERqpiBq1uTAsaX78EwVtjGUuae-XMENXiw&usqp=CAU.png",
    "image_integrity": "sha256-/tih/7ew0eziEZIVD4qoTWb0YrElAuRG3b40SnEstyk=",
    "properties": {
        "simple_property": "TEST NFTs",
        "rich_property": {
            "name": "TEST",
            "value": "1",
            "display_value": "1",
            "class": "emphasis",
            "css": {
                "color": "#ffffff",
                "font-weight": "bold",
                "text-decoration": "underline"
            }
        },
        "array_property": {
            "name": "Artwork",
            "value": [1, 2, 3, 4],
            "class": "emphasis"
        }
    }
}

#converting json to serialized data
metadataStr = json.dumps(info)

print(metadataStr)
# hashing the metadatastr to pass through the nft creation 
hash = hashlib.new("sha512_256")
hash.update(b"arc0003/amj")
hash.update(metadataStr.encode("utf-8"))
metadatahash = hash.digest()

print (metadatahash)
# creating NFT
nft_service.create_nft(metadatahash)


app_id = create_app(client, admin, admin_addr, nft_service.nft_id, 1647864001, 1647865001, 1000, 10 )

print(app_id)
print("ceated")
nft_service.change_nft_credentials_txn(escrow_address =  logic.get_application_address(app_id))
print("nft sent to escrow")
setupAuctionApp(client, app_id, admin, admin, nft_service.nft_id, 1 )
print("auction live")
placeBid(client, app_id, buyer, 1010)
