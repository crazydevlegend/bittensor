import bittensor
from bittensor.utils.router import Router
from bittensor.synapse import Synapse
from bittensor.synapse import SynapseOutput
from bittensor.session import Session

import argparse
import random
import torch
import torch.nn.functional as F
import transformers
from transformers import BertModel, BertConfig
from munch import Munch

class BertSynapseBase (Synapse):
    def __init__(   self,
                config: Munch,
                session: Session):
        r""" Init a new base-bert synapse.

            Args:
                config (:obj:`munch.Munch`, `required`): 
                    BertNSP configuration class.

                Session (:obj:`bittensor.Session`, `optional`): 
                    bittensor training session.

        """
        super(BertSynapseBase, self).__init__(
            config = config,
            session = session)

        # Hugging face config item.
        huggingface_config = BertConfig(    vocab_size=bittensor.__vocab_size__, 
                                            hidden_size=bittensor.__network_dim__, 
                                            num_hidden_layers=config.synapse.num_hidden_layers, 
                                            num_attention_heads=config.synapse.num_attention_heads, 
                                            intermediate_size=bittensor.__network_dim__, 
                                            is_decoder=False)

        # router: (PKM layer) queries network using pooled embeddings as context.
        # [batch_size, bittensor.__network_dim__] -> topk * [batch_size, bittensor.__network_dim__]
        self.router = Router(x_dim=bittensor.__network_dim__, key_dim=100, topk=10)

        # encoder_layer: encodes tokenized sequences to network dim.
        # [batch_size, sequence_len] -> [batch_size, sequence_len, bittensor.__network_dim__]
        self.encoder_transformer = BertModel(huggingface_config, add_pooling_layer=True)

        # context_transformer: distills the remote_context from inputs
        # [batch_size, sequence_len] -> [batch_size, sequence_len, bittensor.__network_dim__]
        self.context_transformer = BertModel(huggingface_config, add_pooling_layer=False)

        # router: (PKM layer) queries network using pooled embeddings as context.
        # [batch_size, bittensor.__network_dim__] -> topk * [batch_size, bittensor.__network_dim__]
        self.router = Router(x_dim=bittensor.__network_dim__, key_dim=100, topk=10)

        # hidden_layer: transforms context and encoding to network_dim hidden units.
        # [batch_size, sequence_dim, 2 * bittensor.__network_dim__] -> [batch_size, sequence_len, bittensor.__network_dim__]
        self.hidden_layer = torch.nn.Linear(2 * bittensor.__network_dim__, bittensor.__network_dim__)

        self.to(self.device)

    @staticmethod
    def add_args(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:    
        r""" Add custom params to the parser.
        """
        parser.add_argument('--synapse.num_hidden_layers', default=2, type=int, 
                            help='Number of hidden layers in the Transformer encoder.')
        parser.add_argument('--synapse.num_attention_heads', default=2, type=int, 
                            help='Number of attention heads for each attention layer in the Transformer encoder.')
        parser.add_argument('--synapse.n_block_filter', default=100, type=int, help='Stale neurons are filtered after this many blocks.')
        return parser

    def forward_text(self, inputs: torch.LongTensor):
        """ Local forward inputs through the BERT NSP Synapse.

            Args:
                inputs (:obj:`torch.LongTensor` of shape :obj:`(batch_size, sequence_len)`, `required`): 
                    Batch_size length list of tokenized sentences.
            
            Returns:
                hidden (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_len, bittensor.__network_dim__)`, `required`): 
                    Hidden layer representation produced using the local_context.
        """
        hidden = self.forward(inputs=inputs, remote = False).local_hidden
        return hidden

    def call_remote(self, inputs, context):
        r""" Makes remote calls to neurons.

            Args:
                inputs (:obj:`torch.LongTensor` of shape :obj:`(batch_size, sequence_len)`, `required`): 
                    Batch_size length list of text sentences.

                context (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, transform_dim)`, `required`): 
                    Per example tensor used to select which neuron to send each example to.
            
            Returns:
                remote_context (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_len, bittensor.__network_dim__)`, `required`): 
                    Joined context vector from remote peer neurons.

                weights (:obj:`torch.LongTensor` of shape :obj:`(batch_size, metagraph.state.n)`, `optional`): 
                    weights for each active neuron.
        """

        # uids: unique keys for peer neurons.
        # uids.shape = [metagraph.n]
        uids = self.session.metagraph.state.uids # Returns a list of neuron uids.
       
        # uids: uids with an emit call in the last 100 blocks.
        # uids = [-1]
        block = self.session.metagraph.state.block 
        emit = self.session.metagraph.state.emit
        staleness = (block - emit)
        uids = uids[torch.where(staleness > self.config.synapse.n_block_filter)] 

        # Return zeros if there are no remaining peer neurons.
        if torch.numel(uids) == 0:
            n = self.session.metagraph.state.n
            remote_context = torch.zeros(size=(inputs.shape[0], inputs.shape[1], bittensor.__network_dim__))
            weights = torch.zeros(size=(inputs.shape[0], n))
            return remote_context, weights

        # neurons: endpoint information for filtered keys.
        # neurons.shape = [len(uids)]
        neurons = self.session.metagraph.state.uids_to_neurons(uids)
        
        # request: inputs routeed to peers using context to filter topk.
        # request.shape = neurons.size * [-1, sequence_dim, channels, rows, cols]
        requests, weights = self.router.route( neurons, context, inputs) 

        # responses: responses from neurons.
        # responses.shape = neurons.size * [-1, sequence_dim, __network_dim__]
        responses = self.session.dendrite.forward_text( neurons, requests )

        # remote_context: Responses weighted and joined along the __network_dim__.
        # remote_context.shape = [batch_size, sequence_dim, bittensor.__network_dim__]
        remote_context = self.router.join( responses )

        # scatter weights back onto shape (bs, n)
        indices = self.session.metagraph.state.uids_to_indices(uids).repeat(inputs.shape[0], 1)
        filled_weights = torch.zeros(inputs.shape[0], self.session.metagraph.state.n)
        filled_weights.scatter_(1, indices, weights)
        return remote_context, filled_weights 

    def forward(self,
                inputs: torch.LongTensor,
                remote: bool = False):
        r""" Forward pass inputs and labels through the NSP BERT module.

            Args:
                inputs (:obj:`torch.LongTensor` of shape :obj:`(batch_size, sequence_len)`, `required`): 
                    Batch_size length list of text sentences.

                remote (:obj:`bool')`, `optional`):
                    Switch to True if this forward pass queries the network for the remote_context.

            bittensor.SynapseOutput ( 
                    loss  (:obj:`List[str]` of shape :obj:`(batch_size)`, `required`):
                        Total loss acumulation used by loss.backward()

                    local_hidden (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_len, bittensor.__network_dim__)`, `required`):
                        Hidden layer encoding produced using local_context.

                    remote_hidden (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_len, bittensor.__network_dim__)`, `optional`): 
                        Hidden layer encoding produced using the remote_context.

                    distillation_loss (:obj:`torch.FloatTensor` of shape :obj:`(1)`, `optional`): 
                        Distillation loss between local_context and remote_context.

                    weights (:obj:`torch.LongTensor` of shape :obj:`(batch_size, metagraph.state.n)`, `optional`): 
                        weights for each active neuron.
                )
        """
        inputs = inputs.to(self.device)
        # Return vars to be filled.
        output = SynapseOutput(loss = torch.tensor(0.0))
   
        # encoding: transformer encoded sentences.
        # encoding.shape = [batch_size, sequence_len, bittensor.__network_dim__]
        encoding = self.encoder_transformer(input_ids=inputs, return_dict=True)
        encoding_hidden = encoding.last_hidden_state
        encoding_pooled = encoding.pooler_output

        # local_context: distilled version of remote_context.
        # local_context.shape = [batch_size, sequence_len, bittensor.__network_dim__]
        local_context = self.context_transformer(input_ids=inputs, return_dict=True).last_hidden_state

        # local_hidden: hidden layer encoding of sequence with local_context.
        # local_hidden.shape = [batch_size, sequence_len, bittensor.__network_dim__]
        local_hidden = torch.cat([encoding_hidden, local_context], dim=2)
        local_hidden = self.hidden_layer(local_hidden)
        output.local_hidden = local_hidden

        if remote:
            output = self.base_remote_forward(local_context, inputs, encoding_pooled, encoding_hidden, output)

        return output

    def base_remote_forward(self, local_context, inputs, encoding_pooled, encoding_hidden, output):
        """Forward pass inputs and labels through the remote BERT networks.

        Args:
            local_context (:obj: `torch.FloatTensor` of shape :obj: `(batch_size, bittensor.__network_dim__)`, `required`)
                    Distillation model for remote_context.
            
            inputs (:obj:`torch.LongTensor` of shape :obj:`(batch_size, sequence_len)`, `required`): 
                    Batch_size length list of text sentences.

            encoding_outputs (:obj:`tuple(torch.FloatTensor)`, `optional`): 
                    This tuple must consist of (last_hidden_state, optional: hidden_states, optional: attentions) 
                    last_hidden_state (torch.FloatTensor of shape (batch_size, sequence_length, hidden_size)) 
                    is a tensor of hidden-states at the output of the last layer of the encoder. Used in the cross-attention of the decoder.
           
            encoding_hidden (:obj:`torch.FloatTensor` of shape (batch_size, sequence_length, hidden_size), `optional`) :
                    Sequence of hidden-states at the output of the last layer of the encoder of the model.

            output (:obj: `Bittensor.SynapseOutput`, `required`)
                    The object containing the output thus far of the local context run

        Returns:
            bittensor.SynapseOutput ( 
                    loss  (:obj:`List[str]` of shape :obj:`(batch_size)`, `required`):
                        Total loss acumulation used by loss.backward()

                    local_hidden (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_len, bittensor.__network_dim__)`, `required`):
                        Hidden layer encoding produced using local_context.

                    remote_hidden (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_len, bittensor.__network_dim__)`, `optional`): 
                        Hidden layer encoding produced using the remote_context.

                    distillation_loss (:obj:`torch.FloatTensor` of shape :obj:`(1)`, `optional`): 
                        Distillation loss between local_context and remote_context.

                    weights (:obj:`torch.LongTensor` of shape :obj:`(batch_size, metagraph.state.n)`, `optional`): 
                        weights for each active neuron.
                )
        """
        # remote_context: joined responses from a bittensor.forward_text call.
        # remote_context.shape = [batch_size, sequence_len, bittensor.__network_dim__]
        remote_context, weights = self.call_remote(inputs, encoding_pooled)
        output.weights = weights
        remote_context = remote_context.to(self.device)

        # distillation_loss: distillation loss between local_context and remote_context
        # distillation_loss.shape = [1]
        distillation_loss = F.mse_loss(local_context, remote_context.detach())
        output.distillation_loss = distillation_loss
        output.loss = output.loss + distillation_loss

        # remote_hidden: hidden layer encoding using remote_context.
        # remote_hidden.shape = [batch_size, sequence_len, bittensor.__network_dim__]
        remote_hidden = torch.cat([encoding_hidden, remote_context], dim=2)
        remote_hidden = self.hidden_layer(remote_hidden)
        output.remote_hidden = remote_hidden

        return output
    
    def remote_forward(self, output, targets):
        """ 

        Args:
            output (bittensor.SynapseOutput): 
                    The output object being populated by the local forward.
            targets (:obj:`torch.FloatTensor`  of shape :obj:`(batch_size, target_dim)`, `optional`, defaults to None): 
                    Image labels.

        Returns:
            output (bittensor.SynapseOutput ( 
                    loss  (:obj:`List[str]` of shape :obj:`(batch_size)`, `required`):
                        Total loss acumulation to be used by loss.backward()

                    local_hidden (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, bittensor.__network_dim__)`, `required`):
                        Hidden layer encoding produced using local_context.

                    local_target (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, target_dim)`, `optional`):
                        FFNN Target predictions using student_context. 

                    local_target_loss (:obj:`torch.FloatTensor` of shape :obj:`(1)`, `optional`): 
                        FFNN Classification loss using student_context.

                    remote_hidden (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, bittensor.__network_dim__)`, `optional`): 
                        Hidden layer encoding produced using the remote_context.

                    remote_target (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, target_dim)`, `optional`):
                        FFNN Target predictions using the remote_context.

                    remote_target_loss (:obj:`torch.FloatTensor` of shape :obj:`(1)`, `optional`):
                        FFNN Classification loss using the remote_context.

                    distillation_loss (:obj:`torch.FloatTensor` of shape :obj:`(1)`, `optional`): 
                        Distillation loss between local_context and remote_context.

                    keys (:obj:`torch.LongTensor` of shape :obj:`(-1)`, `optional`): 
                        Keys for queried neurons.

                    scores (:obj:`torch.LongTensor` of shape :obj:`(batch_size, len(keys))`, `optional`): 
                        scores for each active key per example.
                )
        """
        if targets is not None:
            # remote_target: projection the local_hidden to target dimension.
            # remote_target.shape = [batch_size, 2]
            remote_target = self.target_layer(output.remote_hidden)
            remote_target = F.softmax(remote_target, dim=1)
            output.remote_target = remote_target
            
        return output


class BertNSPSynapse (BertSynapseBase):
    def __init__(   self,
                    config: Munch,
                    session: Session):
        r""" Init a new bert nsp synapse module.

            Args:
                config (:obj:`Munch`, `required`): 
                    BertNSP configuration class.

                session (:obj:`bittensor.Session`, `required`): 
                    bittensor session object. 
        """
        super(BertNSPSynapse, self).__init__(
            config = config,
            session = session)

        # Hugging face config item.
        huggingface_config = BertConfig(    vocab_size=bittensor.__vocab_size__, 
                                            hidden_size=bittensor.__network_dim__, 
                                            num_hidden_layers=config.synapse.num_hidden_layers, 
                                            num_attention_heads=config.synapse.num_attention_heads, 
                                            intermediate_size=bittensor.__network_dim__, 
                                            is_decoder=False)
        
        # target_layer: maps from hidden layer to vocab dimension for each token. Used by MLM loss.
        # [batch_size, sequence_len, bittensor.__network_dim__] -> [batch_size, sequence_len, bittensor.__vocab_size__]
        self.target_layer = transformers.modeling_bert.BertOnlyNSPHead(huggingface_config)

        # Loss function: MLM cross-entropy loss.
        # predicted: [batch_size, sequence_len, 1], targets: [batch_size, sequence_len, 1] -> [1]
        self.loss_fct = torch.nn.CrossEntropyLoss()
  
    @staticmethod
    def add_args(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:    
        r""" Add custom params to the parser.
        """
        parser.add_argument('--synapse.num_hidden_layers', default=2, type=int, 
                            help='Number of hidden layers in the Transformer encoder.')
        parser.add_argument('--synapse.num_attention_heads', default=2, type=int, 
                            help='Number of attention heads for each attention layer in the Transformer encoder.')
        parser.add_argument('--synapse.n_block_filter', default=100, type=int, help='Stale neurons are filtered after this many blocks.')
        return parser
    
    def forward_text(self, inputs: torch.LongTensor):
        """ Local forward inputs through the BERT NSP Synapse.

            Args:
                inputs (:obj:`torch.LongTensor` of shape :obj:`(batch_size, sequence_len)`, `required`): 
                    Batch_size length list of tokenized sentences.
            
            Returns:
                hidden (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_len, bittensor.__network_dim__)`, `required`): 
                    Hidden layer representation produced using the local_context.
        """
        hidden = self.forward(inputs = inputs, remote = False).local_hidden
        return hidden


    def forward(self,
                inputs: torch.LongTensor,
                targets: torch.Tensor = None,
                remote: bool = False):
        r""" Forward pass inputs and labels through the NSP BERT module.

            Args:
                inputs (:obj:`torch.LongTensor` of shape :obj:`(batch_size, sequence_len)`, `required`): 
                    Batch_size length list of text sentences.

                token_type_ids (:obj:`torch.LongTensor` of shape :obj:`(batch_size, sequence_len)`, `optional`): 
                    Token Type IDs for training to distinguish between the sentence context and the next sentence.

                attention_mask (:obj:`torch.LongTensor` of shape :obj:`(batch_size, sequence_len)`, `optional`): 
                    Mask to avoid performing attention on padding token indices.
                    Mask values selected in ``[0, 1]``:
                        - 1 for tokens that are **not masked**,
                        - 0 for tokens that are **maked**.        

                targets (``torch.LongTensor`` of shape ``(batch_size,)``, `optional`):
                    Targets for computing the next sequence prediction (classification) loss. 
                    Indices should be in ``[0, 1]``:
                        - 0 indicates sequence B is a continuation of sequence A,
                        - eqw1 indicates sequence B is a random sequence.

                remote (:obj:`bool')`, `optional`):
                    Switch to True if this forward pass queries the network for the remote_context.

            bittensor.SynapseOutput ( 
                    loss  (:obj:`List[str]` of shape :obj:`(batch_size)`, `required`):
                        Total loss acumulation used by loss.backward()

                    local_hidden (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_len, bittensor.__network_dim__)`, `required`):
                        Hidden layer encoding produced using local_context.

                    local_target (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_len, bittensor.__vocab_size__)`, `optional`):
                        BERT NSP Target predictions produced using local_context. 

                    local_target_loss (:obj:`torch.FloatTensor` of shape :obj:`(1)`, `optional`): 
                        BERT NSP loss using local_context.

                    remote_hidden (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_len, bittensor.__network_dim__)`, `optional`): 
                        Hidden layer encoding produced using the remote_context.

                    remote_target (:obj:`torch.FloatTensor` of shape :obj:`(batch_size,  bittensor.__vocab_size__)`, `optional`):
                        BERT NSP Target predictions using the remote_context.

                    remote_target_loss (:obj:`torch.FloatTensor` of shape :obj:`(1)`, `optional`):
                        BERT NSP loss using the remote_context.

                    distillation_loss (:obj:`torch.FloatTensor` of shape :obj:`(1)`, `optional`): 
                        Distillation loss between local_context and remote_context.

                    weights (:obj:`torch.LongTensor` of shape :obj:`(batch_size, metagraph.state.n)`, `optional`): 
                        weights for each active neuron.
                )
        """
        # Call forward method from bert base.
        output = BertSynapseBase.forward(self, inputs = inputs, remote = remote) 

        if targets is not None:
            # local_target: projection the local_hidden to target dimension.
            # local_target.shape = [batch_size, 2]
            local_target = self.target_layer(output.local_hidden)
            local_target = F.softmax(local_target, dim=1)
            output.local_target = local_target
            
            # local_target_loss: logit(1) > logit(0) if next_inputs are the real next sequences.
            # local_target_loss: [1]
            local_target_loss = self.loss_fct(local_target.view(targets.shape[0], -1), targets)
            output.local_target_loss = local_target_loss
            output.loss = output.loss + local_target_loss


        if remote:
            output = self.remote_forward(output, targets)
            # remote_target_loss: logit(1) > logit(0) if next_inputs are the real next sequences.
            # remote_target_loss: [1]
            remote_target_loss = self.loss_fct(output.remote_target.view(targets.shape[0], -1), targets)
            output.remote_target_loss = remote_target_loss
            output.loss = output.loss + remote_target_loss
        return output
    


class BertMLMSynapse (BertSynapseBase):
    def __init__(   self,
                    config: Munch,
                    session: Session):
        r""" Bert synapse for MLM training

            Args:
                config (:obj:`Munch`, `required`): 
                    BertNSP configuration class.

                session (:obj:`bittensor.Session`, `required`): 
                    bittensor session object. 
        """
        super(BertMLMSynapse, self).__init__(
            config = config,
            session = session)

        # Hugging face config item.
        huggingface_config = BertConfig(    vocab_size=bittensor.__vocab_size__, 
                                            hidden_size=bittensor.__network_dim__, 
                                            num_hidden_layers=config.synapse.num_hidden_layers, 
                                            num_attention_heads=config.synapse.num_attention_heads, 
                                            intermediate_size=bittensor.__network_dim__, 
                                            is_decoder=False)
      
        # target_layer: maps from hidden layer to vocab dimension for each token. Used by MLM loss.
        # [batch_size, sequence_len, bittensor.__network_dim__] -> [batch_size, sequence_len, bittensor.__vocab_size__]
        self.target_layer = transformers.modeling_bert.BertLMPredictionHead(huggingface_config)

        # Loss function: MLM cross-entropy loss.
        # predicted: [batch_size, sequence_len, 1], targets: [batch_size, sequence_len, 1] -> [1]
        self.loss_fct = torch.nn.CrossEntropyLoss()

    @staticmethod
    def add_args(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:    
        r""" Add custom params to the parser.
        """
        parser.add_argument('--synapse.num_hidden_layers', default=2, type=int, 
                            help='Number of hidden layers in the Transformer encoder.')
        parser.add_argument('--synapse.num_attention_heads', default=2, type=int, 
                            help='Number of attention heads for each attention layer in the Transformer encoder.')
        parser.add_argument('--synapse.n_block_filter', default=100, type=int, help='Stale neurons are filtered after this many blocks.')
        return parser

    def forward_text(self, inputs: torch.LongTensor):
        """ Local forward inputs through the BERT NSP Synapse.

            Args:
                inputs (:obj:`torch.LongTensor` of shape :obj:`(batch_size, sequence_len)`, `required`): 
                    Batch_size length list of tokenized sentences.
            
            Returns:
                hidden (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_len, bittensor.__network_dim__)`, `required`): 
                    Hidden layer representation produced using the local_context.
        """
        hidden = self.forward(inputs = inputs, remote = False).local_hidden
        return hidden

    def forward(self,
                inputs: torch.LongTensor,
                targets: torch.LongTensor = None,
                remote: bool = False):
        r""" Forward pass inputs and labels through the MLM BERT module.

            Args:
                inputs (:obj:`torch.LongTensor` of shape ``(batch_size, sequence_length)``, `required`):
                    Batch_size length list of tokenized sentences.
                
                targets (:obj:`torch.LongTensor` of shape ``(batch_size, sequence_length)``, `optional`):
                    Targets for computing the masked language modeling loss.
                    Indices should be in ``[-100, 0, ..., config.vocab_size]`` (see ``input_ids`` docstring)
                    Tokens with indices set to ``-100`` are ignored (masked), the loss is only computed for the tokens with targets
                    in ``[0, ..., config.vocab_size]``   

                remote (:obj:`bool')`, `optional`):
                    Switch to True if this forward pass queries the network for the remote_context.

            bittensor.SynapseOutput  ( 
                    loss  (:obj:`List[str]` of shape :obj:`(batch_size)`, `required`):
                        Total loss acumulation used by loss.backward()

                    local_hidden (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_len, bittensor.__network_dim__)`, `required`):
                        Hidden layer encoding produced using local_context.

                    local_target (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_len, bittensor.__vocab_size__)`, `optional`):
                        BERT MLM Target predictions produced using local_context. 

                    local_target_loss (:obj:`torch.FloatTensor` of shape :obj:`(1)`, `optional`): 
                        BERT MLM loss using local_context.

                    remote_hidden (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_len, bittensor.__network_dim__)`, `optional`): 
                        Hidden layer encoding produced using the remote_context.

                    remote_target (:obj:`torch.FloatTensor` of shape :obj:`(batch_size,  bittensor.__vocab_size__)`, `optional`):
                        BERT MLM Target predictions using the remote_context.

                    remote_target_loss (:obj:`torch.FloatTensor` of shape :obj:`(1)`, `optional`):
                        BERT MLM loss using the remote_context.

                    distillation_loss (:obj:`torch.FloatTensor` of shape :obj:`(1)`, `optional`): 
                        Distillation loss between local_context and remote_context.

                    weights (:obj:`torch.LongTensor` of shape :obj:`(batch_size, metagraph.state.n)`, `optional`): 
                        weights for each active neuron.
                )
        """
        # Call forward method from bert base.
        output = BertSynapseBase.forward(self, inputs = inputs, remote = remote) 

        if targets is not None:
            # local_target: projection the local_hidden to target dimension.
            # local_target.shape = [batch_size, bittensor.__vocab_size__]
            local_target = self.target_layer(output.local_hidden)
            local_target = F.softmax(local_target, dim=1)
            output.local_target = local_target
            
            # local_target_loss: logit(1) > logit(0) if next_inputs are the real next sequences.
            # local_target_loss: [1]
            local_target_loss = self.loss_fct(local_target.view(-1, bittensor.__vocab_size__), targets.view(-1))
            output.local_target_loss = local_target_loss
            output.loss = output.loss + local_target_loss

        if remote:
            output = self.remote_forward(output, targets)
            # remote_target_loss: logit(1) > logit(0) if next_inputs are the real next sequences.
            # remote_target_loss: [1]
            remote_target_loss = self.loss_fct(output.remote_target.view(-1, bittensor.__vocab_size__), targets.view(-1))
            output.remote_target_loss = remote_target_loss
            output.loss = output.loss + remote_target_loss

        return output